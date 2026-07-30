from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.persistence.models import ArtifactRow
from app.domain.events import ExecutionEvent, ObservationCapturedEvent
from app.domain.execution import ObservationRef
from app.services.events import EventWriter


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        sessions: async_sessionmaker[AsyncSession],
        events: EventWriter | None = None,
    ) -> None:
        self.root = root.resolve()
        self.sessions = sessions
        self.events = events
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(
        self,
        *,
        run_id: str,
        task_run_id: str | None,
        artifact_type: str,
        content: bytes,
        mime_type: str,
        extension: str,
        metadata: dict[str, object] | None = None,
        event_builder: Callable[[ArtifactRow], ExecutionEvent] | None = None,
        dedup_key: str | None = None,
    ) -> ArtifactRow:
        artifact_id = str(uuid4())
        run_dir = (self.root / run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / f"{artifact_id}.{extension.lstrip('.')}"
        descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=run_dir)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        relative_path = target.relative_to(self.root).as_posix()
        row = ArtifactRow(
            id=artifact_id,
            test_run_id=run_id,
            task_run_id=task_run_id,
            type=artifact_type,
            relative_path=relative_path,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            metadata_json=metadata or {},
        )
        async with self.sessions() as session:
            session.add(row)
            event_row = None
            if event_builder is not None:
                if self.events is None:
                    raise RuntimeError("ArtifactStore requires EventWriter for event_builder")
                event_row = await self.events.stage(
                    session,
                    event_builder(row),
                    dedup_key=dedup_key,
                )
            await session.commit()
            await session.refresh(row)
        if event_row is not None and self.events is not None:
            await self.events.publish(event_row)
        return row

    async def save_observation(
        self,
        *,
        run_id: str,
        task_run_id: str,
        content: bytes,
        mime_type: str,
        activity: str | None,
        cycle_count: int,
    ) -> ArtifactRow:
        return await self.save(
            run_id=run_id,
            task_run_id=task_run_id,
            artifact_type="screenshot",
            content=content,
            mime_type=mime_type,
            extension="png",
            metadata={"activity": activity, "cycle_count": cycle_count},
            event_builder=lambda row: ObservationCapturedEvent(
                run_id=run_id,
                task_run_id=task_run_id,
                observation=ObservationRef(
                    artifact_id=row.id,
                    mime_type=mime_type,
                    activity=activity,
                    cycle_count=cycle_count,
                ),
            ),
            dedup_key=(
                f"{run_id}:{task_run_id}:{cycle_count}:observation.captured"
            ),
        )

    async def load_content(self, artifact_id: str) -> tuple[bytes, str]:
        async with self.sessions() as session:
            row = await session.get(ArtifactRow, artifact_id)
        if row is None:
            raise LookupError(f"Artifact not found: {artifact_id}")
        return self.resolve(row.relative_path).read_bytes(), row.mime_type

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Artifact path escapes the configured root")
        return candidate
