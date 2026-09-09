from __future__ import annotations

import asyncio
import logging

from app.domain.activity import AgentActivity
from app.domain.execution import TestRun, TestRunSnapshot, TestRunStatus
from app.execution.active_runs import ActiveRunRegistry
from app.execution.executor import RunExecutor
from app.llm import ModelProvider
from app.persistence.test_repository import (
    ActiveRunExists,
    SqlAlchemyTestRepository,
)
from app.prompts import PromptDefinition
from app.tools import MCPToolProvider, snapshot_tools


logger = logging.getLogger(__name__)


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
        tools: MCPToolProvider,
        screenshot_history_rounds: int = 3,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.active_run_registry = active_run_registry
        self.model_provider = model_provider
        self.app_version = app_version
        self.act_prompt = act_prompt
        self.judge_prompt = judge_prompt
        self.screenshot_history_rounds = screenshot_history_rounds
        self.tools = tools
        self._start_lock = asyncio.Lock()
        self._preparations: set[asyncio.Task[None]] = set()
        self._closing = False

    async def start(
        self,
        *,
        test_plan_id: str,
        assumptions_confirmed: bool,
    ) -> TestRun:
        if assumptions_confirmed is not True:
            raise ValueError("plan assumptions must be confirmed")
        async with self._start_lock:
            if self._closing:
                raise RuntimeError("Application is shutting down")
            active_run_id = self.active_run_registry.active_run_id
            if active_run_id is not None:
                raise RunConflict(active_run_id)
            prepared_run: asyncio.Future[TestRun] = asyncio.get_running_loop().create_future()
            preparation = asyncio.create_task(
                self._prepare_and_execute(test_plan_id, prepared_run),
                name=f"prepare-run-{test_plan_id}",
            )
            self._preparations.add(preparation)
            try:
                return await asyncio.shield(prepared_run)
            except asyncio.CancelledError:
                # Keep startup serialized if the HTTP caller disappears.
                preparation.cancel()
                await asyncio.gather(preparation, return_exceptions=True)
                if prepared_run.done() and not prepared_run.cancelled():
                    prepared_run.exception()
                raise

    async def _prepare_and_execute(
        self, test_plan_id: str, prepared_run: asyncio.Future[TestRun]
    ) -> None:
        run: TestRun | None = None
        try:
            async with self.tools.connect() as tools:
                snapshot = TestRunSnapshot(
                    tool_catalog=snapshot_tools(tools),
                    execution_model=self.model_provider.client_for_activity(
                        AgentActivity.EXECUTION
                    ).profile_snapshot,
                    screenshot_history_rounds=self.screenshot_history_rounds,
                    act_prompt_version=self.act_prompt.version,
                    judge_prompt_version=self.judge_prompt.version,
                    app_version=self.app_version,
                    execution_protocol_version="2",
                )
                try:
                    run = await self.repository.create_test_run(
                        test_plan_id=test_plan_id, snapshot=snapshot,
                    )
                except ActiveRunExists as exc:
                    raise RunConflict(exc.active_run_id) from exc
                cancellation = asyncio.Event()
                execution_task = asyncio.current_task()
                assert execution_task is not None
                self._preparations.discard(execution_task)
                self.active_run_registry.register(run.id, execution_task, cancellation)
                prepared_run.set_result(run)
                await self.executor.run(run.id, cancellation, tools)
        except Exception as exc:
            if not prepared_run.done():
                prepared_run.set_exception(exc)
            else:
                logger.exception("Run execution or MCP cleanup failed for %s", run.id if run else test_plan_id)
        except asyncio.CancelledError:
            if not prepared_run.done():
                prepared_run.cancel()
            raise
        finally:
            current = asyncio.current_task()
            if current is not None:
                self._preparations.discard(current)
            if run is not None:
                self.active_run_registry.unregister(run.id)

    async def shutdown(self) -> None:
        self._closing = True
        preparations = tuple(self._preparations)
        for preparation in preparations:
            preparation.cancel()
        await asyncio.gather(*preparations, return_exceptions=True)
        await self.active_run_registry.shutdown()

    async def cancel(self, test_run_id: str) -> bool:
        run = await self.repository.get_test_run(test_run_id)
        if run.status == TestRunStatus.FINISHED:
            return False
        return self.active_run_registry.cancel(test_run_id)

    async def reconcile_orphaned_runs(self) -> None:
        await self.repository.reconcile_orphaned()
