"""TestRun 报告组合与 JSON/HTML 导出应用边界。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import posixpath
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.artifacts import ArtifactStore
from app.config import APP_DESCRIPTION, APP_NAME
from app.domain.execution import (
    Artifact, ArtifactType, ReasonCode, TaskRun, TaskRunStatus, TestRunDetail, TestRunReport, group_events_by_task_run,
)
from app.domain.planning import TestTask
from app.domain.resources import (
    ResourceCatalogSnapshot, ResourceDefinitionSnapshot, is_resource_in_skill_package, top_level_skills,
)


TASK_STATUS_LABELS: dict[TaskRunStatus, str] = {
    TaskRunStatus.RUNNING: "运行中", TaskRunStatus.PASSED: "通过", TaskRunStatus.FAILED: "失败",
    TaskRunStatus.BLOCKED: "阻塞", TaskRunStatus.SKIPPED: "跳过", TaskRunStatus.CANCELLED: "取消",
}
REASON_LABELS: dict[ReasonCode, str] = {
    ReasonCode.COMPLETED: "正常完成", ReasonCode.ASSERTION_FAILED: "成功标准未满足",
    ReasonCode.GOAL_UNREACHABLE: "目标无法达成", ReasonCode.CYCLE_LIMIT: "决策轮次达到上限",
    ReasonCode.GOAL_FAILURE_LIMIT: "Goal 累计失败达到上限", ReasonCode.MODEL_UNAVAILABLE: "模型不可用",
    ReasonCode.INVALID_MODEL_RESPONSE: "模型响应无效", ReasonCode.AGENT_BLOCKED: "任务受阻",
    ReasonCode.TOOL_FAILED: "工具交互失败", ReasonCode.PROCESS_RESTARTED: "进程中断后重启",
    ReasonCode.GLOBAL_FAIL_FAST: "前序任务终止后跳过", ReasonCode.USER_CANCELLED: "用户取消",
    ReasonCode.MODEL_CONTEXT_EXCEEDED: "模型上下文超限", ReasonCode.UNEXPECTED_ERROR: "未处理的执行错误",
}


def format_elapsed(started_at: datetime | None, finished_at: datetime | None) -> str:
    if started_at is None or finished_at is None:
        return "—"
    # Browser Date parses each timestamp at millisecond precision before Math.round.
    started_at = started_at.replace(microsecond=started_at.microsecond // 1000 * 1000)
    finished_at = finished_at.replace(microsecond=finished_at.microsecond // 1000 * 1000)
    seconds = max(0, math.floor((finished_at - started_at).total_seconds() + 0.5))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def relative_resource_path(skill_uri: str, resource_uri: str) -> str:
    """Display a known package member relative to its frozen entry, without filesystem I/O."""
    return posixpath.relpath(unquote(urlsplit(resource_uri).path),
                            posixpath.dirname(unquote(urlsplit(skill_uri).path)))


def number_task_executions(task_runs: list[TaskRun]) -> dict[str, int]:
    execution_counts: dict[str, int] = defaultdict(int)
    execution_numbers: dict[str, int] = {}
    for task_run in task_runs:
        if task_run.started_at is not None:
            execution_counts[task_run.test_task_id] += 1
        execution_numbers[task_run.id] = execution_counts[task_run.test_task_id]
    return execution_numbers


def find_unstarted_tasks(detail: TestRunDetail) -> list[TestTask]:
    recorded_task_ids = {task_run.test_task_id for task_run in detail.task_runs}
    return [task for task in detail.test_plan.content.tasks if task.test_task_id not in recorded_task_ids]


def group_resources_by_skill(
    resource_catalog: ResourceCatalogSnapshot,
) -> dict[ResourceDefinitionSnapshot | None, list[ResourceDefinitionSnapshot]]:
    """Group frozen resource references without copying facts or reading their sources."""
    resources_by_skill: dict[ResourceDefinitionSnapshot | None, list[ResourceDefinitionSnapshot]] = {
        skill: [] for skill in top_level_skills(resource_catalog)
    }
    resources_by_skill[None] = []
    for resource in resource_catalog.resources:
        skill = next((entry for entry in resources_by_skill if entry is not None
                      and entry.source_id == resource.source_id
                      and is_resource_in_skill_package(entry.uri, resource.uri)), None)
        if skill is None:
            resources_by_skill[None].append(resource)
        elif resource.uri != skill.uri:
            resources_by_skill[skill].append(resource)
    return resources_by_skill


class ReportService:
    """Render and store a complete report supplied by the export route."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts
        self.templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
            autoescape=select_autoescape(["html"]),
        )
        self.templates.globals.update(
            app_name=APP_NAME,
            app_description=APP_DESCRIPTION,
            task_status_labels=TASK_STATUS_LABELS,
            reason_labels=REASON_LABELS,
        )
        self.templates.filters.update(
            group_events_by_task_run=group_events_by_task_run,
            number_task_executions=number_task_executions,
            find_unstarted_tasks=find_unstarted_tasks,
            group_resources_by_skill=group_resources_by_skill,
            format_elapsed=format_elapsed,
            relative_resource_path=relative_resource_path,
        )

    async def export(self, report: TestRunReport, export_format: str) -> Artifact:
        if export_format == "json":
            report_dump = report.model_dump(mode="json")
            content = json.dumps(
                report_dump, ensure_ascii=False, indent=2
            ).encode("utf-8")
            artifact_type = ArtifactType.JSON_EXPORT
            mime_type, extension = "application/json", "json"
        elif export_format == "html":
            images = [
                artifact
                for artifact in report.artifacts
                if artifact.type == ArtifactType.IMAGE
            ]
            loaded = await asyncio.gather(
                *(self._load_image_data_url(artifact.id) for artifact in images)
            )
            image_data_urls = {
                artifact.id: image_data_url
                for artifact, image_data_url in zip(images, loaded, strict=True)
            }
            content = self.templates.get_template("report.html").render(
                report=report,
                image_data_urls=image_data_urls,
            ).encode("utf-8")
            artifact_type = ArtifactType.HTML_EXPORT
            mime_type, extension = "text/html; charset=utf-8", "html"
        else:
            raise ValueError("format must be json or html")
        return await self.artifacts.save_export(
            test_run_id=report.detail.run.id,
            artifact_type=artifact_type,
            content=content,
            mime_type=mime_type,
            extension=extension,
        )

    async def _load_image_data_url(self, artifact_id: str) -> str:
        try:
            raw, mime_type = await self.artifacts.load_content(artifact_id)
        except FileNotFoundError:
            raw = self.templates.get_template("image-unavailable.svg").render().encode("utf-8")
            mime_type = "image/svg+xml"
        return f"data:{mime_type};base64," + base64.b64encode(raw).decode("ascii")
