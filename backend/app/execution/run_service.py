from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.domain.activity import AgentActivity
from app.domain.execution import TestRun, TestRunSnapshot, TestRunStatus
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


@dataclass(eq=False)
class _RunWork:
    """One slot from preparation through MCP cleanup; ID exists only after creation."""

    task: asyncio.Task[None] | None = None
    test_run_id: str | None = None
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)


class RunService:
    """TestRun 命令入口和活跃运行生命周期的唯一所有者。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyTestRepository,
        executor: RunExecutor,
        model_provider: ModelProvider,
        app_version: str,
        act_prompt: PromptDefinition,
        judge_prompt: PromptDefinition,
        tools: MCPToolProvider,
        screenshot_history_rounds: int = 3,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.model_provider = model_provider
        self.app_version = app_version
        self.act_prompt = act_prompt
        self.judge_prompt = judge_prompt
        self.screenshot_history_rounds = screenshot_history_rounds
        self.tools = tools
        self._start_lock = asyncio.Lock()
        self._work: _RunWork | None = None
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
            active_run_id = self._work.test_run_id if self._work is not None else None
            if active_run_id is not None:
                raise RunConflict(active_run_id)
            prepared_run: asyncio.Future[TestRun] = asyncio.get_running_loop().create_future()
            work = _RunWork()
            self._work = work
            work.task = asyncio.create_task(
                self._prepare_and_execute(test_plan_id, prepared_run, work),
                name=f"prepare-run-{test_plan_id}",
            )

            def release_work(completed: asyncio.Task[None]) -> None:
                # Startup failures use prepared_run. Execution reporting failures
                # and post-execution resource cleanup failures are logged once.
                try:
                    completed.result()
                except asyncio.CancelledError as exc:
                    if exc.__cause__ is not None or (exc.__context__ is not None and not exc.__suppress_context__):
                        logger.error("Run externally cancelled with unreported cleanup failure: %s", work.test_run_id,
                                     exc_info=(type(exc), exc, exc.__traceback__))
                except BaseException as exc:
                    logger.error("Background execution finalization or MCP cleanup failed: %s", work.test_run_id,
                                 exc_info=(type(exc), exc, exc.__traceback__))
                if not prepared_run.done():
                    prepared_run.cancel()
                if self._work is work:
                    self._work = None

            work.task.add_done_callback(release_work)
            try:
                return await asyncio.shield(prepared_run)
            except asyncio.CancelledError:
                # Keep startup serialized if the HTTP caller disappears.
                work.cancellation.set()
                if work.test_run_id is None:
                    work.task.cancel()
                await asyncio.gather(work.task, return_exceptions=True)
                if prepared_run.done() and not prepared_run.cancelled():
                    prepared_run.exception()
                raise

    async def _prepare_and_execute(
        self, test_plan_id: str, prepared_run: asyncio.Future[TestRun], work: _RunWork,
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
                    creation = asyncio.create_task(self.repository.create_test_run(
                        test_plan_id=test_plan_id, snapshot=snapshot,
                    ))
                    try:
                        run = await asyncio.shield(creation)
                    except asyncio.CancelledError:
                        # A committed run must acquire an executor even when its
                        # HTTP caller disappears before receiving the result.
                        work.cancellation.set()
                        run = await creation
                except ActiveRunExists as exc:
                    raise RunConflict(exc.active_run_id) from exc
                assert run is not None
                work.test_run_id = run.id
                prepared_run.set_result(run)
                await self.executor.run(run.id, work.cancellation, tools)
        except Exception as exc:
            if not prepared_run.done():
                prepared_run.set_exception(exc)
                return
            raise
        except asyncio.CancelledError:
            if not prepared_run.done():
                prepared_run.cancel()
            raise

    async def shutdown(self) -> None:
        self._closing = True
        work = self._work
        if work is not None and work.task is not None:
            work.cancellation.set()
            if work.test_run_id is None:
                work.task.cancel()
            await asyncio.gather(work.task, return_exceptions=True)

    async def cancel(self, test_run_id: str) -> bool:
        run = await self.repository.get_test_run(test_run_id)
        if run.status == TestRunStatus.FINISHED:
            return False
        work = self._work
        if work is None or work.test_run_id != test_run_id:
            return False
        work.cancellation.set()
        return True

    async def reconcile_orphaned_runs(self) -> None:
        await self.repository.reconcile_orphaned()
