"""运行图片与报告导出文件的本地存储边界。"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.domain.execution import Artifact, ArtifactType
from app.domain.ids import ArtifactId, TaskRunId
from app.persistence.mappers import artifact_from_row
from app.persistence.models import ArtifactRow, TaskRunRow


class ArtifactStore:
    """拥有图片/导出 bytes 与 typed 数据库元数据之间的一致写入。"""

    def __init__(
        self,
        root: Path,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.root = root.resolve()
        self.sessions = sessions
        self.root.mkdir(parents=True, exist_ok=True)

    async def save_image(
        self,
        *,
        task_run_id: str,
        content: bytes,
        mime_type: str,
    ) -> Artifact:
        if not mime_type.startswith("image/") or not content:
            raise ValueError("Images require non-empty image content")
        return await self.save(
            test_run_id=None,
            task_run_id=task_run_id,
            artifact_type=ArtifactType.IMAGE,
            content=content,
            mime_type=mime_type,
            extension=mimetypes.guess_extension(mime_type) or ".image",
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
        if artifact_type == ArtifactType.IMAGE:
            raise ValueError("save_export does not accept image artifacts")
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

    async def load_image_for_task(self, artifact_id: ArtifactId, *, task_run_id: TaskRunId) -> tuple[bytes, str]:
        """Read an existing image owned by the calling task's run, preserving its identity."""
        image_task_run = aliased(TaskRunRow)
        calling_run_id = select(TaskRunRow.test_run_id).where(TaskRunRow.id == task_run_id).scalar_subquery()
        async with self.sessions() as session:
            row = await session.scalar(
                select(ArtifactRow).join(image_task_run, ArtifactRow.task_run_id == image_task_run.id).where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.type == ArtifactType.IMAGE,
                    image_task_run.test_run_id == calling_run_id,
                )
            )
        if row is None:
            raise LookupError(f"Image artifact {artifact_id} is not available in the current run")
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
