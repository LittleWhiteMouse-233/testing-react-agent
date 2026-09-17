from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution import ArtifactType, ReasonCode
from app.domain.errors import TestCaseHasActiveRun, TestPlanNotLatest
from app.domain.execution import (
    CycleStartedEvent,
    ExecutionErrorEvent,
    RunEvent,
    RunLifecycleEvent,
    StoredRunEvent,
    TaskLifecycleEvent,
    TasksSkippedEvent,
)
from app.domain.execution import (
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
    TestRun,
    TestRunDetail,
    TestRunEnvironmentSnapshot,
    TestRunStatus,
    TestRunVerdict,
)
from app.domain.planning import (
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTask,
    TestTaskDefinition,
    identify_plan_content,
)
from app.domain.planning import TestCase, TestCaseContent
from app.persistence.adapters import (
    dump_planning_context,
    dump_string_list,
    dump_task_run_result,
    dump_test_run_environment_snapshot,
    load_test_run_environment_snapshot,
)
from app.persistence.mappers import (
    task_run_from_row,
    test_case_from_row,
    test_plan_from_rows,
    test_run_from_row,
)
from app.persistence.models import (
    ArtifactRow,
    TaskRunRow,
    TestCaseRow,
    TestPlanRow,
    TestRunRow,
    TestTaskRow,
)
from app.event_stream.writer import EventWriter
from app.persistence.db import read_snapshot


def now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyRunRepository:
    """Canonical entity repository and execution transaction boundary."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        events: EventWriter,
    ) -> None:
        self.sessions = sessions
        self.events = events

    async def get_test_case(self, test_case_id: str) -> TestCase:
        async with self.sessions() as session:
            row = await self._require_available_test_case(session, test_case_id)
        return test_case_from_row(row)

    @staticmethod
    async def _require_available_test_case(session: AsyncSession, test_case_id: str) -> TestCaseRow:
        row = await session.scalar(select(TestCaseRow).where(
            TestCaseRow.id == test_case_id, TestCaseRow.is_archived.is_(False),
        ))
        if row is None:
            raise LookupError("用例不存在或已归档")
        return row

    async def update_test_case(self, test_case_id: str, content: TestCaseContent) -> TestCase:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await self._require_available_test_case(session, test_case_id)
            row.name = content.name
            row.source_text = content.source_text
            await session.commit()
        return test_case_from_row(row)

    async def archive_test_case(self, test_case_id: str) -> None:
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await self._require_available_test_case(session, test_case_id)
            active_run_id = await session.scalar(
                select(TestRunRow.id).join(TestPlanRow).where(
                    TestPlanRow.test_case_id == test_case_id,
                    TestRunRow.status.in_([TestRunStatus.PENDING, TestRunStatus.RUNNING]),
                ).limit(1)
            )
            if active_run_id is not None:
                raise TestCaseHasActiveRun("用例存在待执行或运行中的任务，请先取消并等待结束")
            row.is_archived = True
            await session.commit()

    async def create_plan(
        self,
        *,
        test_case_id: str,
        draft: TestPlanContent[TestTaskDefinition],
        planning_context: TestPlanPlanningContext,
        origin: TestPlanOrigin,
        derived_from_plan_id: str | None = None,
    ) -> TestPlan:
        identified = identify_plan_content(draft)
        async with self.sessions() as session:
            # Serialize latest-version selection with insertion. SQLite's default
            # deferred transaction would otherwise allow concurrent writers to
            # select the same version or the same manual-revision parent.
            await session.execute(text("BEGIN IMMEDIATE"))
            await self._require_available_test_case(session, test_case_id)
            latest = await session.scalar(
                select(TestPlanRow)
                .where(TestPlanRow.test_case_id == test_case_id)
                .order_by(TestPlanRow.version_number.desc())
                .limit(1)
            )
            if origin != TestPlanOrigin.PLANNING:
                if latest is None or latest.id != derived_from_plan_id:
                    raise TestPlanNotLatest("计划版本已变化，请刷新后基于最新计划重试")
            elif derived_from_plan_id is not None:
                raise ValueError("initial planning must not have a parent plan")
            elif latest is not None:
                raise TestPlanNotLatest("已有计划生成，请刷新后填写额外输入重新规划")
            row = TestPlanRow(
                id=str(uuid4()),
                test_case_id=test_case_id,
                version_number=(latest.version_number + 1 if latest else 1),
                origin=origin,
                derived_from_plan_id=derived_from_plan_id,
                title=identified.title,
                setup_steps_json=dump_string_list(identified.setup_steps),
                assumptions_json=dump_string_list(identified.assumptions),
                planning_context_json=dump_planning_context(planning_context),
            )
            session.add(row)
            task_rows = [
                TestTaskRow(
                    id=task.test_task_id,
                    test_plan_id=row.id,
                    position=position,
                    type=task.definition.type,
                    title=task.definition.title,
                    goal=task.definition.goal,
                    success_criteria_json=dump_string_list(
                        task.definition.success_criteria
                    ),
                    max_cycles=task.definition.max_cycles,
                )
                for position, task in enumerate(identified.tasks)
            ]
            session.add_all(task_rows)
            await session.commit()
        return test_plan_from_rows(row, task_rows)

    async def get_test_plan(self, test_plan_id: str) -> TestPlan:
        async with self.sessions() as session:
            row = await session.get(TestPlanRow, test_plan_id)
            if row is None:
                raise LookupError("Test plan not found")
            await self._require_available_test_case(session, row.test_case_id)
            tasks = await self._task_rows(session, test_plan_id)
        return test_plan_from_rows(row, tasks)

    async def get_latest_test_plan(self, test_case_id: str) -> TestPlan | None:
        async with self.sessions() as session:
            await self._require_available_test_case(session, test_case_id)
            row = await session.scalar(
                select(TestPlanRow)
                .where(TestPlanRow.test_case_id == test_case_id)
                .order_by(TestPlanRow.version_number.desc())
                .limit(1)
            )
            if row is None:
                return None
            return test_plan_from_rows(row, await self._task_rows(session, row.id))

    async def create_test_run(
        self,
        *,
        test_plan_id: str,
        environment: TestRunEnvironmentSnapshot,
    ) -> TestRun:
        async with self.sessions() as session:
            # The partial unique index is the final invariant; the immediate
            # transaction also turns the expected concurrent loser into the
            # domain-level ActiveRunExists outcome instead of an IntegrityError.
            await session.execute(text("BEGIN IMMEDIATE"))
            plan = await session.get(TestPlanRow, test_plan_id)
            if plan is None:
                raise LookupError("Test plan not found")
            await self._require_available_test_case(session, plan.test_case_id)
            latest_id = await session.scalar(
                select(TestPlanRow.id)
                .where(TestPlanRow.test_case_id == plan.test_case_id)
                .order_by(TestPlanRow.version_number.desc())
                .limit(1)
            )
            if latest_id != plan.id:
                raise ValueError("only the latest test plan may be started")
            active = await session.scalar(
                select(TestRunRow).where(
                    TestRunRow.status.in_(
                        [TestRunStatus.PENDING, TestRunStatus.RUNNING]
                    )
                )
            )
            if active is not None:
                raise ActiveRunExists(active.id)
            row = TestRunRow(
                id=str(uuid4()),
                test_plan_id=test_plan_id,
                status=TestRunStatus.PENDING,
                environment_json=dump_test_run_environment_snapshot(environment),
            )
            session.add(row)
            await session.commit()
        return test_run_from_row(row)

    async def get_test_run(self, test_run_id: str) -> TestRun:
        async with self.sessions() as session:
            row = await session.get(TestRunRow, test_run_id)
        if row is None:
            raise LookupError("Test run not found")
        return test_run_from_row(row)

    async def get_test_run_detail(self, test_run_id: str) -> TestRunDetail:
        async with read_snapshot(self.sessions) as session:
            return await read_test_run_detail(session, test_run_id)

    async def start_run(self, test_run_id: str) -> None:
        event = RunLifecycleEvent(
            type="run.started", test_run_id=test_run_id, task_run_id=None
        )

        async def mutation(session: AsyncSession) -> None:
            row = await self._require_run(session, test_run_id)
            row.status = TestRunStatus.RUNNING
            row.started_at = now()

        await self.events.commit(
            event, mutation, dedup_key=f"{test_run_id}:run.started"
        )

    async def start_task(self, test_run_id: str, task: TestTask) -> TaskRun:
        task_run_id = str(uuid4())
        event = TaskLifecycleEvent(
            type="task.started",
            test_run_id=test_run_id,
            task_run_id=task_run_id,
        )

        async def mutation(session: AsyncSession) -> TaskRunRow:
            row = TaskRunRow(
                id=task_run_id,
                test_run_id=test_run_id,
                test_task_id=task.test_task_id,
                status=TaskRunStatus.RUNNING,
                cycle_count=0,
                started_at=now(),
            )
            session.add(row)
            await session.flush()
            return row

        row, _ = await self.events.commit(
            event,
            mutation,
            dedup_key=f"{test_run_id}:{task_run_id}:task.started",
        )
        return task_run_from_row(row)

    async def update_cycle(self, task_run_id: str, cycle_count: int) -> None:
        async with self.sessions() as session:
            current = await self._require_task_run(session, task_run_id)
            test_run_id = current.test_run_id
        event = CycleStartedEvent(
            test_run_id=test_run_id,
            task_run_id=task_run_id,
            cycle_count=cycle_count,
        )

        async def mutation(session: AsyncSession) -> None:
            row = await self._require_task_run(session, task_run_id)
            row.cycle_count = cycle_count

        await self.events.commit(
            event,
            mutation,
            dedup_key=f"{test_run_id}:{task_run_id}:{cycle_count}:cycle.started",
        )

    async def finish_task(
        self,
        task_run_id: str,
        *,
        status: TaskRunStatus,
        result: TaskRunResult,
        cycle_count: int,
    ) -> None:
        async with self.sessions() as session:
            current = await self._require_task_run(session, task_run_id)
            test_run_id = current.test_run_id
        event = TaskLifecycleEvent(
            type="task.finished",
            test_run_id=test_run_id,
            task_run_id=task_run_id,
        )

        async def mutation(session: AsyncSession) -> None:
            row = await self._require_task_run(session, task_run_id)
            is_user_cancelled = result.reason_code == ReasonCode.USER_CANCELLED
            if (status == TaskRunStatus.CANCELLED) != is_user_cancelled:
                raise ValueError(
                    "cancelled task run and USER_CANCELLED reason must occur together"
                )
            evidence_ids = set(result.evidence_artifact_ids)
            if len(evidence_ids) != len(result.evidence_artifact_ids):
                raise ValueError("task result evidence ids must be unique")
            if status in {TaskRunStatus.PASSED, TaskRunStatus.FAILED} and not evidence_ids:
                raise ValueError("passed/failed task run requires screenshot evidence")
            if evidence_ids:
                owned_ids = set(
                    (
                        await session.scalars(
                            select(ArtifactRow.id).where(
                                ArtifactRow.id.in_(evidence_ids),
                                ArtifactRow.task_run_id == task_run_id,
                                ArtifactRow.type == ArtifactType.SCREENSHOT,
                            )
                        )
                    ).all()
                )
                if owned_ids != evidence_ids:
                    raise ValueError(
                        "task result evidence must be screenshots owned by the task run"
                    )
            row.status = status
            row.result_json = dump_task_run_result(result)
            row.cycle_count = cycle_count
            row.finished_at = now()

        await self.events.commit(
            event,
            mutation,
            dedup_key=f"{test_run_id}:{task_run_id}:task.finished",
        )

    async def skip_remaining(
        self, test_run_id: str, tasks: Sequence[TestTask]
    ) -> list[TaskRun]:
        timestamp = now()
        rows = [
            TaskRunRow(
                id=str(uuid4()),
                test_run_id=test_run_id,
                test_task_id=task.test_task_id,
                status=TaskRunStatus.SKIPPED,
                cycle_count=0,
                result_json=dump_task_run_result(
                    TaskRunResult(
                        reason_code=ReasonCode.GLOBAL_FAIL_FAST,
                        summary="Skipped by global fail-fast",
                        evidence_artifact_ids=[],
                    )
                ),
                finished_at=timestamp,
            )
            for task in tasks
        ]
        event = TasksSkippedEvent(
            test_run_id=test_run_id,
            task_run_id=None,
            task_run_ids=[row.id for row in rows],
        )

        async def mutation(session: AsyncSession) -> list[TaskRunRow]:
            session.add_all(rows)
            await session.flush()
            return rows

        persisted, _ = await self.events.commit(
            event,
            mutation,
            dedup_key=f"{test_run_id}:tasks.skipped",
        )
        return [task_run_from_row(row) for row in persisted]

    async def list_task_runs(self, test_run_id: str) -> list[TaskRun]:
        async with self.sessions() as session:
            rows = await self._task_run_rows(session, test_run_id)
        return [task_run_from_row(row) for row in rows]

    async def finish_run(
        self, test_run_id: str, verdict: TestRunVerdict
    ) -> None:
        event_type = "run.cancelled" if verdict == TestRunVerdict.CANCELLED else "run.finished"
        event = RunLifecycleEvent(
            type=event_type, test_run_id=test_run_id, task_run_id=None
        )

        async def mutation(session: AsyncSession) -> None:
            row = await self._require_run(session, test_run_id)
            row.status = TestRunStatus.FINISHED
            row.verdict = verdict
            row.finished_at = now()

        await self.events.commit(
            event, mutation, dedup_key=f"{test_run_id}:{event_type}"
        )

    async def append_event(
        self, event: RunEvent, *, dedup_key: str | None = None
    ) -> StoredRunEvent | None:
        return await self.events.append(event, dedup_key=dedup_key)

    async def append_events(
        self, events: Sequence[tuple[RunEvent, str | None]]
    ) -> list[StoredRunEvent]:
        return await self.events.append_many(events)

    async def reconcile_orphaned(self) -> list[str]:
        async with self.sessions() as session:
            ids = list(
                (
                    await session.scalars(
                        select(TestRunRow.id).where(
                            TestRunRow.status.in_(
                                [TestRunStatus.PENDING, TestRunStatus.RUNNING]
                            )
                        )
                    )
                ).all()
            )
        for test_run_id in ids:
            task_runs = await self.list_task_runs(test_run_id)
            for task in task_runs:
                if task.status == TaskRunStatus.RUNNING:
                    await self.finish_task(
                        task.id,
                        status=TaskRunStatus.BLOCKED,
                        result=TaskRunResult(
                            reason_code=ReasonCode.PROCESS_RESTARTED,
                            summary="Process restarted before the task completed",
                            evidence_artifact_ids=[],
                        ),
                        cycle_count=task.cycle_count,
                    )
            await self.append_event(
                ExecutionErrorEvent(
                    test_run_id=test_run_id,
                    task_run_id=None,
                    reason_code=ReasonCode.PROCESS_RESTARTED,
                    message="Process restarted before execution completed",
                ),
                dedup_key=f"{test_run_id}:process_restarted",
            )
            await self.finish_run(test_run_id, TestRunVerdict.BLOCKED)
        return ids

    @staticmethod
    async def _task_rows(
        session: AsyncSession, test_plan_id: str
    ) -> list[TestTaskRow]:
        return list(
            (
                await session.scalars(
                    select(TestTaskRow)
                    .where(TestTaskRow.test_plan_id == test_plan_id)
                    .order_by(TestTaskRow.position)
                )
            ).all()
        )

    @staticmethod
    async def _task_run_rows(
        session: AsyncSession, test_run_id: str
    ) -> list[TaskRunRow]:
        return list(
            (
                await session.scalars(
                    select(TaskRunRow)
                    .join(TestTaskRow, TestTaskRow.id == TaskRunRow.test_task_id)
                    .where(TaskRunRow.test_run_id == test_run_id)
                    .order_by(TestTaskRow.position)
                )
            ).all()
        )

    @staticmethod
    async def _require_run(session: AsyncSession, test_run_id: str) -> TestRunRow:
        row = await session.get(TestRunRow, test_run_id)
        if row is None:
            raise LookupError("Test run not found")
        return row

    @staticmethod
    async def _require_task_run(
        session: AsyncSession, task_run_id: str
    ) -> TaskRunRow:
        row = await session.get(TaskRunRow, task_run_id)
        if row is None:
            raise LookupError("Task run not found")
        return row


class ActiveRunExists(RuntimeError):
    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"Run {active_run_id} is already active")
        self.active_run_id = active_run_id


async def read_test_run_detail(session: AsyncSession, test_run_id: str) -> TestRunDetail:
    run_row = await session.get(TestRunRow, test_run_id)
    if run_row is None:
        raise LookupError("Test run not found")
    plan_row = await session.get(TestPlanRow, run_row.test_plan_id)
    if plan_row is None:
        raise RuntimeError("Test run references a missing test plan")
    plan_tasks = await SqlAlchemyRunRepository._task_rows(session, plan_row.id)
    task_rows = await SqlAlchemyRunRepository._task_run_rows(session, test_run_id)
    return TestRunDetail(
        run=test_run_from_row(run_row),
        test_plan=test_plan_from_rows(plan_row, plan_tasks),
        environment=load_test_run_environment_snapshot(run_row.environment_json),
        task_runs=[task_run_from_row(row) for row in task_rows],
    )
