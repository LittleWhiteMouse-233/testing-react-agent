from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.persistence.models import (
    ArtifactRow,
    StepEventRow,
    TaskRunRow,
    TestRunRow,
)
from app.services.artifacts import ArtifactStore
from app.services.events import serialize_event


def artifact_dict(row: ArtifactRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.test_run_id,
        "task_run_id": row.task_run_id,
        "type": row.type,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "metadata": row.metadata_json,
        "created_at": row.created_at.isoformat(),
        "url": f"/api/artifacts/{row.id}",
    }


class ReportService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        artifacts: ArtifactStore,
    ) -> None:
        self.sessions = sessions
        self.artifacts = artifacts
        self.templates = Environment(
            loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
            autoescape=select_autoescape(["html"]),
        )

    async def build(self, run_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if not run:
                raise LookupError("Run not found")
            tasks = list(
                (
                    await session.scalars(
                        select(TaskRunRow)
                        .where(TaskRunRow.test_run_id == run_id)
                        .order_by(TaskRunRow.task_index)
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(StepEventRow)
                        .where(StepEventRow.test_run_id == run_id)
                        .order_by(StepEventRow.sequence)
                    )
                ).all()
            )
            artifacts = list(
                (
                    await session.scalars(
                        select(ArtifactRow)
                        .where(ArtifactRow.test_run_id == run_id)
                        .order_by(ArtifactRow.created_at)
                    )
                ).all()
            )
        artifact_items = [artifact_dict(row) for row in artifacts]
        task_items = []
        for row in tasks:
            outcome = row.outcome_json
            evidence_ids = set(
                outcome.get("evidence_artifact_ids", []) if outcome else []
            )
            task_items.append(
                {
                    "id": row.id,
                    "task_index": row.task_index,
                    "definition": row.task_json,
                    "status": row.status,
                    "cycle_count": row.cycle_count,
                    "outcome": outcome,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "artifacts": [
                        item for item in artifact_items if item["task_run_id"] == row.id
                    ],
                    "evidence": [
                        item for item in artifact_items if item["id"] in evidence_ids
                    ],
                }
            )
        return {
            "run": {
                "id": run.id,
                "status": run.status,
                "overall_result": run.overall_result,
                "device_id": run.device_id,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "created_at": run.created_at.isoformat(),
            },
            "snapshot": run.snapshot_json,
            "summary": {
                "task_count": len(tasks),
                "passed": sum(item.status == "passed" for item in tasks),
                "failed": sum(item.status == "failed" for item in tasks),
                "blocked": sum(item.status == "blocked" for item in tasks),
                "skipped": sum(item.status == "skipped" for item in tasks),
                "event_count": len(events),
                "artifact_count": len(artifacts),
            },
            "tasks": task_items,
            "events": [serialize_event(row) for row in events],
            "artifacts": artifact_items,
        }

    async def export(self, run_id: str, export_format: str) -> ArtifactRow:
        report = await self.build(run_id)
        if export_format == "json":
            content = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
            mime_type, extension = "application/json", "json"
        elif export_format == "html":
            enriched = json.loads(json.dumps(report))
            for artifact in enriched["artifacts"]:
                if artifact["type"] == "screenshot":
                    async with self.sessions() as session:
                        row = await session.get(ArtifactRow, artifact["id"])
                    if row is None:
                        raise LookupError(f"Artifact not found: {artifact['id']}")
                    raw = self.artifacts.resolve(row.relative_path).read_bytes()
                    artifact["data_url"] = (
                        f"data:{row.mime_type};base64,"
                        + base64.b64encode(raw).decode("ascii")
                    )
            by_id = {item["id"]: item for item in enriched["artifacts"]}
            for task in enriched["tasks"]:
                task["artifacts"] = [
                    by_id[item["id"]] for item in task["artifacts"] if item["id"] in by_id
                ]
            content = self.templates.get_template("report.html").render(
                report=enriched
            ).encode("utf-8")
            mime_type, extension = "text/html; charset=utf-8", "html"
        else:
            raise ValueError("format must be json or html")
        return await self.artifacts.save(
            run_id=run_id,
            task_run_id=None,
            artifact_type=f"{export_format}_export",
            content=content,
            mime_type=mime_type,
            extension=extension,
        )
