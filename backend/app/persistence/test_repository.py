from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.execution import Artifact, ArtifactType, ReasonCode
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
    TestRunSnapshot,
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
    dump_test_run_snapshot,
    load_test_run_snapshot,
)
from app.persistence.mappers import (
    artifact_from_row,
    task_run_from_row,
    test_case_from_row,
    test_plan_from_rows,
    test_run_from_row,
)
from app.persistence.models import (
    ArtifactRow,
    RunEventRow,
    TaskRunRow,
    TestCaseRow,
    TestPlanRow,
    TestRunRow,
    TestTaskRow,
)
from app.event_stream.writer import EventWriter
from app.persistence.mappers import stored_event_from_row


def now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyTestRepository:
    """Canonical entity repository and execution transaction boundary."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        events: EventWriter,
    ) -> None:
        self.sessions = sessions
        self.events = events

    async def create_test_case(self, content: TestCaseContent) -> TestCase:
        row = TestCaseRow(
            id=str(uuid4()), name=content.name, source_text=content.source_text
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return test_case_from_row(row)

    async def get_test_case(self, test_case_id: str) -> TestCase:
        async with self.sessions() as session:
            row = await session.get(TestCaseRow, test_case_id)
        if row is None:
            raise LookupError("Test case not found")
        return test_case_from_row(row)

    async def list_test_cases(
        self, *, limit: int, offset: int
    ) -> tuple[list[TestCase], int]:
        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(TestCaseRow))
            rows = list(
                (
                    await session.scalars(
                        select(TestCaseRow)
                        .order_by(TestCaseRow.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
        return [test_case_from_row(row) for row in rows], int(total or 0)

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
            if await session.get(TestCaseRow, test_case_id) is None:
                raise LookupError("Test case not found")
            latest = await session.scalar(
                select(TestPlanRow)
                .where(TestPlanRow.test_case_id == test_case_id)
                .order_by(TestPlanRow.version_number.desc())
                .limit(1)
            )
            if origin == TestPlanOrigin.MANUAL_REVISION:
                if latest is None or latest.id != derived_from_plan_id:
                    raise ValueError("manual revision must derive from the latest plan")
            elif derived_from_plan_id is not None:
                raise ValueError("only manual revisions have a parent plan")
            else:
                # Planning may finish concurrently, so the definitive origin
                # must be chosen beside the serialized version allocation.
                origin = (
                    TestPlanOrigin.REPLANNING
                    if latest is not None
                    else TestPlanOrigin.PLANNING
                )
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
            tasks = await self._task_rows(session, test_plan_id)
        return test_plan_from_rows(row, tasks)

    async def list_test_plans(self, test_case_id: str) -> list[TestPlan]:
        async with self.sessions() as session:
            if await session.get(TestCaseRow, test_case_id) is None:
                raise LookupError("Test case not found")
            rows = list(
                (
                    await session.scalars(
                        select(TestPlanRow)
                        .where(TestPlanRow.test_case_id == test_case_id)
                        .order_by(TestPlanRow.version_number.desc())
                    )
                ).all()
            )
            plans = [
                test_plan_from_rows(row, await self._task_rows(session, row.id))
                for row in rows
            ]
        return plans

    async def create_test_run(
        self,
        *,
        test_plan_id: str,
        snapshot: TestRunSnapshot,
    ) -> TestRun:
        async with self.sessions() as session:
            # The partial unique index is the final invariant; the immediate
            # transaction also turns the expected concurrent loser into the
            # domain-level ActiveRunExists outcome instead of an IntegrityError.
            await session.execute(text("BEGIN IMMEDIATE"))
            plan = await session.get(TestPlanRow, test_plan_id)
            if plan is None:
                raise LookupError("Test plan not found")
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
                snapshot_json=dump_test_run_snapshot(snapshot),
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

    async def list_test_runs(
        self,
        *,
        test_case_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[TestRun], int]:
        statement = select(TestRunRow)
        count = select(func.count()).select_from(TestRunRow)
        if test_case_id is not None:
            statement = statement.join(TestPlanRow).where(
                TestPlanRow.test_case_id == test_case_id
            )
            count = count.join(TestPlanRow).where(
                TestPlanRow.test_case_id == test_case_id
            )
        async with self.sessions() as session:
            total = await session.scalar(count)
            rows = list(
                (
                    await session.scalars(
                        statement.order_by(TestRunRow.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
        return [test_run_from_row(row) for row in rows], int(total or 0)

    async def get_test_run_detail(self, test_run_id: str) -> TestRunDetail:
        async with self.sessions() as session:
            run_row = await session.get(TestRunRow, test_run_id)
            if run_row is None:
                raise LookupError("Test run not found")
            plan_row = await session.get(TestPlanRow, run_row.test_plan_id)
            if plan_row is None:
                raise RuntimeError("Test run references a missing test plan")
            plan_tasks = await self._task_rows(session, plan_row.id)
            task_rows = await self._task_run_rows(session, test_run_id)
        return TestRunDetail(
            run=test_run_from_row(run_row),
            test_plan=test_plan_from_rows(plan_row, plan_tasks),
            snapshot=load_test_run_snapshot(run_row.snapshot_json),
            task_runs=[task_run_from_row(row) for row in task_rows],
        )

    async def list_events(
        self, test_run_id: str, *, after: int = 0
    ) -> list[StoredRunEvent]:
        async with self.sessions() as session:
            if await session.get(TestRunRow, test_run_id) is None:
                raise LookupError("Test run not found")
            rows = list(
                (
                    await session.scalars(
                        select(RunEventRow)
                        .where(
                            RunEventRow.test_run_id == test_run_id,
                            RunEventRow.sequence > after,
                        )
                        .order_by(RunEventRow.sequence)
                    )
                ).all()
            )
        return [stored_event_from_row(row) for row in rows]

    async def list_artifacts(self, test_run_id: str) -> list[Artifact]:
        task_ids = select(TaskRunRow.id).where(TaskRunRow.test_run_id == test_run_id)
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ArtifactRow)
                        .where(
                            (ArtifactRow.test_run_id == test_run_id)
                            | (ArtifactRow.task_run_id.in_(task_ids))
                        )
                        .order_by(ArtifactRow.created_at)
                    )
                ).all()
            )
        return [artifact_from_row(row) for row in rows]

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
