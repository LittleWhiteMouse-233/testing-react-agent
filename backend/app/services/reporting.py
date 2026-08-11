from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.domain.artifacts import Artifact, ArtifactType
from app.persistence.execution_repository import SqlAlchemyExecutionRepository
from app.services.artifacts import ArtifactStore
from app.services.read_models import TestRunReport


class ReportService:
    def __init__(
        self,
        repository: SqlAlchemyExecutionRepository,
        artifacts: ArtifactStore,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
            autoescape=select_autoescape(["html"]),
        )

    async def build(self, test_run_id: str) -> TestRunReport:
        return await self.repository.get_report(test_run_id)

    async def export(self, test_run_id: str, export_format: str) -> Artifact:
        report = await self.build(test_run_id)
        report_dump = report.model_dump(mode="json")
        if export_format == "json":
            content = json.dumps(
                report_dump, ensure_ascii=False, indent=2
            ).encode("utf-8")
            artifact_type = ArtifactType.JSON_EXPORT
            mime_type, extension = "application/json", "json"
        elif export_format == "html":
            screenshots = [
                artifact
                for artifact in report.artifacts
                if artifact.type == ArtifactType.SCREENSHOT
            ]
            loaded = await asyncio.gather(
                *(self.artifacts.load_content(artifact.id) for artifact in screenshots)
            )
            image_data_urls = {
                artifact.id: (
                    f"data:{mime_type};base64,"
                    + base64.b64encode(raw).decode("ascii")
                )
                for artifact, (raw, mime_type) in zip(screenshots, loaded, strict=True)
            }
            content = self.templates.get_template("report.html").render(
                report=report_dump,
                image_data_urls=image_data_urls,
            ).encode("utf-8")
            artifact_type = ArtifactType.HTML_EXPORT
            mime_type, extension = "text/html; charset=utf-8", "html"
        else:
            raise ValueError("format must be json or html")
        return await self.artifacts.save_export(
            test_run_id=test_run_id,
            artifact_type=artifact_type,
            content=content,
            mime_type=mime_type,
            extension=extension,
        )
