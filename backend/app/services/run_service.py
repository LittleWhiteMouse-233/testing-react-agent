from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.models import OverallResult, PlanOutput, RunStatus, TaskStatus
from app.graph.executor import AgentGraphExecutor
from app.persistence.models import (
    PlanRevisionRow,
    TaskRunRow,
    TestCaseRow,
    TestRunRow,
)
from app.llm.contracts import LLMProvider
from app.services.events import EventWriter
from app.services.registry import RunRegistry


class RunConflict(RuntimeError):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run {active_run_id} is already active")
        self.active_run_id = active_run_id


def prompt_versions() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1] / "prompts"
    return {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for path in root.glob("*.txt")
    }


class RunService:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        executor: AgentGraphExecutor,
        registry: RunRegistry,
        events: EventWriter,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.executor = executor
        self.registry = registry
        self.events = events
        self.llm = llm
        self.settings = settings
        self._start_lock = asyncio.Lock()

    async def start(
        self,
        *,
        plan_revision_id: str,
        device_id: str,
        confirmed_assumptions: list[str],
    ) -> TestRunRow:
        async with self._start_lock:
            async with self.sessions() as session:
                active = await session.scalar(
                    select(TestRunRow)
                    .where(
                        TestRunRow.status.in_(
                            [RunStatus.PENDING.value, RunStatus.RUNNING.value]
                        )
                    )
                    .order_by(TestRunRow.created_at)
                )
                if active:
                    raise RunConflict(active.id)
                revision = await session.get(PlanRevisionRow, plan_revision_id)
                if not revision:
                    raise LookupError("Plan revision not found")
                test_case = await session.get(TestCaseRow, revision.test_case_id)
                if not test_case:
                    raise LookupError("Test case not found")
                if device_id not in self.executor.devices:
                    raise LookupError("Device not found")
                plan = PlanOutput.model_validate(revision.plan_json)
                if sorted(confirmed_assumptions) != sorted(plan.assumptions):
                    raise ValueError("All plan assumptions must be confirmed exactly once")
                run = TestRunRow(
                    id=str(uuid4()),
                    test_case_id=test_case.id,
                    plan_revision_id=revision.id,
                    device_id=device_id,
                    status=RunStatus.PENDING.value,
                    overall_result=None,
                    snapshot_json={
                        "test_case": {
                            "id": test_case.id,
                            "name": test_case.name,
                            "source_text": test_case.source_text,
                        },
                        "plan_revision": {
                            "id": revision.id,
                            "revision": revision.revision,
                            "source": revision.source,
                        },
                        "plan": plan.model_dump(mode="json"),
                        "device_id": device_id,
                        "model": self.llm.model_info,
                        "enabled_tools": [
                            tool.model_dump(mode="json")
                            for tool in self.executor.tools.list_tools()
                        ],
                        "prompt_versions": prompt_versions(),
                        "confirmed_assumptions": confirmed_assumptions,
                        "app_version": self.settings.app_version,
                    },
                )
                session.add(run)
                await session.commit()
                await session.refresh(run)
            task = asyncio.create_task(
                self.executor.run(run.id), name=f"test-run-{run.id}"
            )
            self.registry.register(run.id, task)
            return run

    async def cancel(self, run_id: str) -> bool:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if not run:
                raise LookupError("Run not found")
            if run.status not in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
                return False
        return self.registry.cancel(run_id)

    async def reconcile_orphaned_runs(self) -> None:
        timestamp = datetime.now(timezone.utc)
        orphan_ids: list[str] = []
        async with self.sessions() as session:
            runs = list(
                (
                    await session.scalars(
                        select(TestRunRow).where(
                            TestRunRow.status.in_(
                                [RunStatus.PENDING.value, RunStatus.RUNNING.value]
                            )
                        )
                    )
                ).all()
            )
            for run in runs:
                run.status = RunStatus.FINISHED.value
                run.overall_result = OverallResult.BLOCKED.value
                run.finished_at = timestamp
                tasks = list(
                    (
                        await session.scalars(
                            select(TaskRunRow).where(
                                TaskRunRow.test_run_id == run.id
                            )
                        )
                    ).all()
                )
                for task in tasks:
                    if task.status == TaskStatus.RUNNING.value:
                        task.status = TaskStatus.BLOCKED.value
                        task.summary = "Process restarted before action result was known"
                        task.finished_at = timestamp
                    elif task.status == TaskStatus.PENDING.value:
                        task.status = TaskStatus.SKIPPED.value
                        task.summary = "Skipped after process restart"
                        task.finished_at = timestamp
                orphan_ids.append(run.id)
            await session.commit()
        for run_id in orphan_ids:
            await self.events.append(
                run_id=run_id,
                event_type="error",
                payload={
                    "classification": "ProcessRestarted",
                    "message": "Automatic recovery is deferred to phase C",
                },
                dedup_key=f"{run_id}:process_restart",
            )
            await self.events.append(
                run_id=run_id,
                event_type="run_finished",
                payload={
                    "status": RunStatus.FINISHED.value,
                    "overall_result": OverallResult.BLOCKED.value,
                },
                dedup_key=f"{run_id}:run_finished",
            )
