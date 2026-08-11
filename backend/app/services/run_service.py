from __future__ import annotations

import asyncio

from app.config import Settings
from app.device.contracts import DeviceProvider
from app.domain.activity import AgentActivity
from app.domain.device import DeviceEnvironmentSnapshot, DeviceHealth
from app.domain.execution import TestRun, TestRunSnapshot, TestRunStatus
from app.execution.executor import RunExecutor
from app.llm.registry import ModelRegistry
from app.persistence.execution_repository import (
    ActiveRunExists,
    SqlAlchemyExecutionRepository,
)
from app.services.prompt_versions import prompt_version
from app.services.registry import RunRegistry
from app.tools import ToolProvider


class RunConflict(RuntimeError):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run {active_run_id} is already active")
        self.active_run_id = active_run_id


class RunService:
    def __init__(
        self,
        *,
        repository: SqlAlchemyExecutionRepository,
        executor: RunExecutor,
        registry: RunRegistry,
        model_registry: ModelRegistry,
        settings: Settings,
        devices: dict[str, DeviceProvider],
        tools: ToolProvider,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.registry = registry
        self.model_registry = model_registry
        self.settings = settings
        self.devices = devices
        self.tools = tools

    async def start(
        self,
        *,
        test_plan_id: str,
        device_id: str,
        assumptions_confirmed: bool,
    ) -> TestRun:
        if assumptions_confirmed is not True:
            raise ValueError("plan assumptions must be confirmed")
        device = self.devices.get(device_id)
        if device is None:
            raise LookupError("Device not found")

        health_result, info_result = await asyncio.gather(
            device.health(), device.describe(), return_exceptions=True
        )
        health = (
            health_result
            if isinstance(health_result, DeviceHealth)
            else DeviceHealth(available=False, message=str(health_result))
        )
        info = None if isinstance(info_result, BaseException) else info_result
        snapshot = TestRunSnapshot(
            device_environment=DeviceEnvironmentSnapshot(
                provider=device.provider,
                info=info,
                health=health,
            ),
            tool_catalog=self.tools.snapshot_for(device_id),
            act_model=self.model_registry.for_activity(
                AgentActivity.ACT
            ).profile_snapshot,
            judge_model=self.model_registry.for_activity(
                AgentActivity.JUDGE
            ).profile_snapshot,
            act_prompt_version=prompt_version("act"),
            judge_prompt_version=prompt_version("judge"),
            app_version=self.settings.app_version,
            execution_protocol_version="1",
        )
        try:
            run = await self.repository.create_test_run(
                test_plan_id=test_plan_id,
                device_id=device_id,
                snapshot=snapshot,
            )
        except ActiveRunExists as exc:
            raise RunConflict(exc.active_run_id) from exc
        task = asyncio.create_task(
            self.executor.run(run.id), name=f"test-run-{run.id}"
        )
        self.registry.register(run.id, task)
        return run

    async def cancel(self, test_run_id: str) -> bool:
        run = await self.repository.get_test_run(test_run_id)
        if run.status == TestRunStatus.FINISHED:
            return False
        return self.registry.cancel(test_run_id)

    async def reconcile_orphaned_runs(self) -> None:
        await self.repository.reconcile_orphaned()
