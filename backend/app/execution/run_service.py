from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.domain.activity import AgentActivity
from app.domain.errors import describe_exception
from app.domain.execution import TestRun, TestRunEnvironmentSnapshot, TestRunStatus
from app.domain.planning import TestTaskType
from app.execution.executor import RunExecutor
from app.llm import ModelProvider
from app.persistence.run_repository import (
    ActiveRunExists,
    SqlAlchemyRunRepository,
)
from app.prompts import PromptCatalog
from app.tools import MCPToolProvider, snapshot_tools
from app.mcp import MCPClientProvider, MCPConnectionError
from app.skill import SkillResourceProvider
from app.test_case_lock import TestCaseLock


logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run {active_run_id} is already active")
        self.active_run_id = active_run_id


@dataclass(eq=False)
class _RunWork:
    """One active run from discovery through terminal persistence."""

    test_case_id: str
    task: asyncio.Task[None] | None = None
    test_run_id: str | None = None
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)


class RunService:
    """TestRun 命令入口和活跃运行生命周期的唯一所有者。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyRunRepository,
        test_case_lock: TestCaseLock,
        executor: RunExecutor,
        model_provider: ModelProvider,
        prompt_catalog: PromptCatalog,
        mcp_clients: MCPClientProvider,
        tools: MCPToolProvider,
        skills: SkillResourceProvider,
        max_cycles_by_task_type: dict[TestTaskType, int],
        app_version: str,
        discovery_timeout_seconds: float,
    ) -> None:
        self.repository = repository
        self.test_case_lock = test_case_lock
        self.executor = executor
        self.model_provider = model_provider
        self.prompt_catalog = prompt_catalog
        self.tools = tools
        self.skills = skills
        self.mcp_clients = mcp_clients
        self.max_cycles_by_task_type = max_cycles_by_task_type
        self.app_version = app_version
        self.discovery_timeout_seconds = discovery_timeout_seconds
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
            # Accepting start reserves this case before MCP preparation. Archive
            # shares this command boundary and cannot overtake an accepted start.
            plan = await self.repository.get_test_plan(test_plan_id)
            self.test_case_lock.acquire(plan.test_case_id, "running")
            prepared_run: asyncio.Future[TestRun] = asyncio.get_running_loop().create_future()
            work = _RunWork(test_case_id=plan.test_case_id)
            self._work = work
            work.task = asyncio.create_task(
                self._prepare_and_execute(test_plan_id, prepared_run, work),
                name=f"prepare-run-{test_plan_id}",
            )

            def release_work(completed: asyncio.Task[None]) -> None:
                # Startup failures use prepared_run; unreportable execution failures are logged once.
                try:
                    completed.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    logger.error("Background execution finalization failed: %s", work.test_run_id,
                                 exc_info=(type(exc), exc, exc.__traceback__))
                if not prepared_run.done():
                    prepared_run.cancel()
                if self._work is work:
                    self._work = None
                    self.test_case_lock.release(work.test_case_id, "running")

            work.task.add_done_callback(release_work)
            try:
                return await asyncio.shield(prepared_run)
            except asyncio.CancelledError:
                # Keep startup serialized if the HTTP caller disappears.
                work.cancellation.set()
                if work.test_run_id is None and not work.task.cancelling():
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
            discovery_phase = "MCP configuration loading"
            try:
                async with asyncio.timeout(self.discovery_timeout_seconds):
                    await self.mcp_clients.load_clients()
                    discovery_phase = "MCP tool discovery"
                    tools = await self.tools.discover_tools()
                    discovery_phase = "Skill and resource discovery"
                    resource_catalog = await self.skills.discover_resources()
            except Exception as exc:
                raise MCPConnectionError(describe_exception(exc, phase=discovery_phase)) from exc
            environment = TestRunEnvironmentSnapshot(
                tool_catalog=snapshot_tools(tools),
                resource_catalog=resource_catalog,
                execution_model=self.model_provider.client_for_activity(
                    AgentActivity.EXECUTION
                ).profile_snapshot,
                max_cycles_by_task_type=self.max_cycles_by_task_type,
                prompt_fingerprints_by_task_type=self.prompt_catalog.prompt_fingerprints_by_task_type,
                app_version=self.app_version,
                execution_protocol_version="8",
            )
            try:
                creation = asyncio.create_task(self.repository.create_test_run(
                    test_plan_id=test_plan_id, environment=environment,
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
            await self.executor.run(run.id, work.cancellation, tools + self.skills.build_resource_tools())
        except Exception as exc:
            if not prepared_run.done():
                prepared_run.set_exception(exc)
                return
            raise

    async def shutdown(self) -> None:
        self._closing = True
        work = self._work
        if work is not None and work.task is not None:
            logger.info("Requesting run cancellation at the next safe boundary: %s", work.test_run_id)
            work.cancellation.set()
            if work.test_run_id is None and not work.task.cancelling():
                work.task.cancel()
            await asyncio.gather(work.task, return_exceptions=True)
            logger.info("Run shutdown wait ended: %s", work.test_run_id)

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
