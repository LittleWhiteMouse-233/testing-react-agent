from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from collections.abc import Sequence

from app.domain.activity import AgentActivity
from app.domain.errors import describe_exception
from app.domain.execution import TestRun, TestRunEnvironmentSnapshot, TestRunStatus
from app.config import SettingsService
from app.execution.executor import RunExecutor
from app.persistence.run_repository import (
    ActiveRunExists,
    SqlAlchemyRunRepository,
)
from app.resources.prompts import PromptCatalog
from app.resources.capabilities import CapabilityProvider
from app.domain.resources import CapabilityColumnSelection
from app.test_case_lock import TestCaseLock


logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    def __init__(self, active_run_id: str | None) -> None:
        super().__init__(f"Run {active_run_id} is already active" if active_run_id is not None
                         else "Run preparation is already in progress")
        self.active_run_id = active_run_id


@dataclass(eq=False)
class _RunWork:
    """One active run from admission through terminal persistence."""

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
        settings_service: SettingsService,
        prompt_catalog: PromptCatalog,
        capabilities: CapabilityProvider,
        app_version: str,
    ) -> None:
        self.repository = repository
        self.test_case_lock = test_case_lock
        self.executor = executor
        self.settings_service = settings_service
        self.prompt_catalog = prompt_catalog
        self.capabilities = capabilities
        self.app_version = app_version
        self._work: _RunWork | None = None
        self._closing = False

    async def start(
        self,
        *,
        test_case_id: str,
        test_plan_id: str,
        assumptions_confirmed: bool,
        selected_columns: Sequence[CapabilityColumnSelection],
    ) -> TestRun:
        if assumptions_confirmed is not True:
            raise ValueError("plan assumptions must be confirmed")
        if self._closing:
            raise RuntimeError("Application is shutting down")
        if self._work is not None:
            raise RunConflict(self._work.test_run_id)
        self.settings_service.ensure_available()
        prepared_run: asyncio.Future[TestRun] = asyncio.get_running_loop().create_future()
        work = _RunWork()
        # Admission and registration cannot yield before both occupations are held.
        self.test_case_lock.acquire(test_case_id, "running", "pre_running")
        try:
            work.task = asyncio.create_task(
                self._prepare_and_execute(test_case_id, test_plan_id, tuple(selected_columns), prepared_run, work),
                name=f"prepare-run-{test_plan_id}",
            )
        except BaseException:
            self.test_case_lock.release(test_case_id, "running", "pre_running")
            raise
        self._work = work

        def release_work(completed: asyncio.Task[None]) -> None:
            # Startup failures use prepared_run; unreportable execution failures are logged once.
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                if not prepared_run.done() and isinstance(exc, Exception):
                    prepared_run.set_exception(exc)
                else:
                    logger.error("Background execution finalization failed: %s", work.test_run_id,
                                 exc_info=(type(exc), exc, exc.__traceback__))
            if not prepared_run.done():
                prepared_run.cancel()
            self.test_case_lock.release(test_case_id, "running", "pre_running")
            if self._work is work:
                self._work = None

        work.task.add_done_callback(release_work)
        try:
            return await asyncio.shield(prepared_run)
        except asyncio.CancelledError:
            # Wait for preparation cleanup or committed-run finalization.
            work.cancellation.set()
            if work.test_run_id is None and not work.task.cancelling():
                work.task.cancel()
            await asyncio.gather(work.task, return_exceptions=True)
            if prepared_run.done() and not prepared_run.cancelled():
                prepared_run.exception()
            raise

    async def _prepare_and_execute(
        self, test_case_id: str, test_plan_id: str, selected_columns: Sequence[CapabilityColumnSelection],
        prepared_run: asyncio.Future[TestRun],
        work: _RunWork,
    ) -> None:
        settings = self.settings_service.current
        execution_settings = settings.execution.model_copy(deep=True)
        model_profile = settings.llm.profile_for_activity(AgentActivity.EXECUTION).model_copy(deep=True)
        await self.repository.validate_test_plan_for_run(test_case_id=test_case_id, test_plan_id=test_plan_id)
        discovery = await self.capabilities.discover(selected_columns=selected_columns, build_execution_tools=True)
        capability_catalog = discovery.catalog
        tools = tuple(tool for tool in discovery.tools
                      if capability_catalog.tool_is_available(str((tool.metadata or {})["tool_source"])))
        environment = TestRunEnvironmentSnapshot(
            capability_catalog=capability_catalog,
            execution_model=model_profile,
            execution=execution_settings,
            prompt_fingerprints_by_task_type=self.prompt_catalog.prompt_fingerprints_by_task_type,
            app_version=self.app_version,
            execution_protocol_version="12",
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
        work.test_run_id = run.id
        self.test_case_lock.release(test_case_id, "pre_running")
        prepared_run.set_result(run)
        await self.executor.run(run.id, work.cancellation, tools, environment)

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
