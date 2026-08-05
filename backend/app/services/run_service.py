from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.device.contracts import DeviceController
from app.domain.execution import (
    DeviceSnapshot,
    ModelSnapshot,
    PlanRevisionSnapshot,
    RunModelsSnapshot,
    RunSnapshot,
    RunStatus,
    TestCaseSnapshot,
)
from app.domain.planning import PlanOutput
from app.domain.planning import TaskType
from app.execution.executor import RunExecutor
from app.execution.ports import ExecutionRepository
from app.llm.contracts import ModelActivity
from app.llm.registry import ModelRegistry
from app.persistence.models import PlanRevisionRow, TestCaseRow, TestRunRow
from app.services.registry import RunRegistry
from app.services.tools import ToolBuildRequest, ToolProvider


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
        repository: ExecutionRepository,
        executor: RunExecutor,
        registry: RunRegistry,
        model_registry: ModelRegistry,
        settings: Settings,
        devices: dict[str, DeviceController],
        tools: ToolProvider,
    ) -> None:
        self.sessions = sessions
        self.repository = repository
        self.executor = executor
        self.registry = registry
        self.model_registry = model_registry
        self.settings = settings
        self.devices = devices
        self.tools = tools
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
                if revision is None:
                    raise LookupError("Plan revision not found")
                test_case = await session.get(TestCaseRow, revision.test_case_id)
                if test_case is None:
                    raise LookupError("Test case not found")
                plan = PlanOutput.model_validate(revision.plan_json)
            if sorted(confirmed_assumptions) != sorted(plan.assumptions):
                raise ValueError("All plan assumptions must be confirmed exactly once")
            device = self.devices.get(device_id)
            if device is None:
                raise LookupError("Device not found")
            health_message = ""
            capabilities = None
            try:
                health = await device.health()
                health_message = health.message
                if health.available:
                    candidate = await device.capabilities()
                    if candidate.screenshot:
                        capabilities = candidate
                    else:
                        health_message = "Device does not support screenshots"
            except Exception as exc:
                health_message = str(exc)
            enabled_tool_names: list[str] = []
            if capabilities is not None:
                configured = frozenset(self.settings.enabled_tool_names)
                act_tools = await self.tools.build_tools(
                    ToolBuildRequest(
                        activity=TaskType.ACT,
                        device=device,
                        capabilities=capabilities,
                        enabled_external_tool_names=configured,
                    )
                )
                judge_tools = await self.tools.build_tools(
                    ToolBuildRequest(
                        activity=TaskType.JUDGE,
                        device=device,
                        capabilities=capabilities,
                        enabled_external_tool_names=configured,
                    )
                )
                enabled_tool_names = sorted(act_tools.names | judge_tools.names)
            act_provider = self.model_registry.for_activity(ModelActivity.ACT)
            judge_provider = self.model_registry.for_activity(ModelActivity.JUDGE)
            snapshot = RunSnapshot(
                test_case=TestCaseSnapshot(
                    id=test_case.id,
                    name=test_case.name,
                    source_text=test_case.source_text,
                ),
                plan_revision=PlanRevisionSnapshot(
                    id=revision.id,
                    revision=revision.revision,
                    source=revision.source,
                ),
                plan=plan,
                confirmed_assumptions=confirmed_assumptions,
                device=DeviceSnapshot(
                    id=device_id,
                    health_message=health_message,
                    capabilities=capabilities,
                ),
                models=RunModelsSnapshot(
                    planning=ModelSnapshot.model_validate(revision.model_info_json),
                    act=act_provider.model_snapshot.model_copy(deep=True),
                    judge=judge_provider.model_snapshot.model_copy(deep=True),
                ),
                enabled_tool_names=enabled_tool_names,
                prompt_versions=prompt_versions(),
                app_version=self.settings.app_version,
            )
            run = TestRunRow(
                id=str(uuid4()),
                test_case_id=test_case.id,
                plan_revision_id=revision.id,
                device_id=device_id,
                status=RunStatus.PENDING.value,
                overall_result=None,
                snapshot_json=snapshot.model_dump(mode="json"),
            )
            async with self.sessions() as session:
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
            if run is None:
                raise LookupError("Run not found")
            if run.status not in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
                return False
        return self.registry.cancel(run_id)

    async def reconcile_orphaned_runs(self) -> None:
        await self.repository.reconcile_orphaned()
