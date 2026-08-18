from __future__ import annotations

import asyncio

from app.device.contracts import DeviceProvider
from app.domain.activity import AgentActivity
from app.domain.resources.device import DeviceEnvironmentSnapshot, DeviceHealth
from app.domain.execution import TestRun, TestRunSnapshot, TestRunStatus
from app.execution.active_runs import ActiveRunRegistry
from app.execution.executor import RunExecutor
from app.llm import ModelProvider
from app.persistence.test_repository import (
    ActiveRunExists,
    SqlAlchemyTestRepository,
)
from app.prompts import PromptDefinition
from app.tools import ToolProvider


class RunConflict(RuntimeError):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run {active_run_id} is already active")
        self.active_run_id = active_run_id


class RunService:
    """TestRun 命令入口和活跃运行生命周期的唯一所有者。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyTestRepository,
        executor: RunExecutor,
        active_run_registry: ActiveRunRegistry,
        model_provider: ModelProvider,
        app_version: str,
        act_prompt: PromptDefinition,
        judge_prompt: PromptDefinition,
        devices: dict[str, DeviceProvider],
        tools: ToolProvider,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.active_run_registry = active_run_registry
        self.model_provider = model_provider
        self.app_version = app_version
        self.act_prompt = act_prompt
        self.judge_prompt = judge_prompt
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
            act_model=self.model_provider.client_for_activity(
                AgentActivity.ACT
            ).profile_snapshot,
            judge_model=self.model_provider.client_for_activity(
                AgentActivity.JUDGE
            ).profile_snapshot,
            act_prompt_version=self.act_prompt.version,
            judge_prompt_version=self.judge_prompt.version,
            app_version=self.app_version,
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
        run_cancellation_event = asyncio.Event()
        execution_task = asyncio.create_task(
            self._execute_registered_run(run.id, run_cancellation_event),
            name=f"test-run-{run.id}",
        )
        self.active_run_registry.register(
            run.id,
            execution_task,
            run_cancellation_event,
        )
        return run

    async def _execute_registered_run(
        self,
        test_run_id: str,
        run_cancellation_event: asyncio.Event,
    ) -> None:
        try:
            await self.executor.run(test_run_id, run_cancellation_event)
        finally:
            self.active_run_registry.unregister(test_run_id)

    async def cancel(self, test_run_id: str) -> bool:
        run = await self.repository.get_test_run(test_run_id)
        if run.status == TestRunStatus.FINISHED:
            return False
        return self.active_run_registry.cancel(test_run_id)

    async def reconcile_orphaned_runs(self) -> None:
        await self.repository.reconcile_orphaned()
