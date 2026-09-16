"""TestRun 报告组合与 JSON/HTML 导出应用边界。"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from collections.abc import Awaitable, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict

from app.artifacts import ArtifactStore
from app.domain.execution import Artifact, ArtifactType, StoredRunEvent, TestRunDetail


class TestRunReport(BaseModel):
    """报告事实组合；canonical detail 中的事实不会在顶层复制。

    在线读取的 events 为请求游标之后的历史；默认读取及导出包含完整历史。
    detail 与 artifacts 始终完整，并与返回的 events 来自同一读取快照。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: TestRunDetail
    events: list[StoredRunEvent]
    artifacts: list[Artifact]


class ReportService:
    """组合 repository 查询事实，并从同一 typed report 生成在线与导出结果。"""

    def __init__(
        self,
        read_report: Callable[[str], Awaitable[TestRunReport]],
        artifacts: ArtifactStore,
    ) -> None:
        self.read_report = read_report
        self.artifacts = artifacts
        self.templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
            autoescape=select_autoescape(["html"]),
        )

    async def build(self, test_run_id: str) -> TestRunReport:
        return await self.read_report(test_run_id)

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
