from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.artifacts import Artifact, ArtifactType
from app.persistence.mappers import artifact_from_row
from app.persistence.models import ArtifactRow


class ArtifactStore:
    """Local-file authority for evidence/export bytes plus typed DB metadata."""

    def __init__(
        self,
        root: Path,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.root = root.resolve()
        self.sessions = sessions
        self.root.mkdir(parents=True, exist_ok=True)

    async def save_screenshot(
        self,
        *,
        task_run_id: str,
        content: bytes,
        mime_type: str,
    ) -> Artifact:
        return await self.save(
            test_run_id=None,
            task_run_id=task_run_id,
            artifact_type=ArtifactType.SCREENSHOT,
            content=content,
            mime_type=mime_type,
            extension="png",
        )

    async def save_export(
        self,
        *,
        test_run_id: str,
        artifact_type: ArtifactType,
        content: bytes,
        mime_type: str,
        extension: str,
    ) -> Artifact:
        if artifact_type == ArtifactType.SCREENSHOT:
            raise ValueError("save_export does not accept screenshot artifacts")
        return await self.save(
            test_run_id=test_run_id,
            task_run_id=None,
            artifact_type=artifact_type,
            content=content,
            mime_type=mime_type,
            extension=extension,
        )

    async def save(
        self,
        *,
        test_run_id: str | None,
        task_run_id: str | None,
        artifact_type: ArtifactType,
        content: bytes,
        mime_type: str,
        extension: str,
    ) -> Artifact:
        if (test_run_id is None) == (task_run_id is None):
            raise ValueError("artifact requires exactly one owner")
        artifact_id = str(uuid4())
        owner = test_run_id or task_run_id
        assert owner is not None
        owner_dir = (self.root / owner).resolve()
        if self.root not in owner_dir.parents:
            raise ValueError("artifact owner path escapes the configured root")
        target = owner_dir / f"{artifact_id}.{extension.lstrip('.')}"
        await asyncio.to_thread(self._atomic_write, target, content)
        row = ArtifactRow(
            id=artifact_id,
            test_run_id=test_run_id,
            task_run_id=task_run_id,
            type=artifact_type,
            relative_path=target.relative_to(self.root).as_posix(),
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        try:
            async with self.sessions() as session:
                session.add(row)
                await session.commit()
        except Exception:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise
        return artifact_from_row(row)

    async def load_content(self, artifact_id: str) -> tuple[bytes, str]:
        async with self.sessions() as session:
            row = await session.get(ArtifactRow, artifact_id)
        if row is None:
            raise LookupError(f"Artifact not found: {artifact_id}")
        content = await asyncio.to_thread(self.resolve(row.relative_path).read_bytes)
        return content, row.mime_type

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Artifact path escapes the configured root")
        return candidate

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
