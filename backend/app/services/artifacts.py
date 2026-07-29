from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.persistence.models import ArtifactRow


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.root = root.resolve()
        self.sessions = sessions
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
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Artifact path escapes the configured root")
        return candidate

