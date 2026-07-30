from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ReasonCode
from app.domain.events import (
    ExecutionErrorEvent,
    ExecutionEvent,
    RunFinishedEvent,
)
from app.domain.execution import (
    OverallResult,
    RunSnapshot,
    RunStatus,
    TaskExecution,
    TaskOutcome,
    TaskStatus,
)
from app.domain.planning import Task
from app.persistence.models import StepEventRow, TaskRunRow, TestRunRow
from app.services.events import EventWriter, serialize_event


def now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyExecutionRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        events: EventWriter,
    ) -> None:
        self.sessions = sessions
        self.events = events

    async def load_snapshot(self, run_id: str) -> RunSnapshot:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
        if run is None:
            raise LookupError(f"Run not found: {run_id}")
        return RunSnapshot.model_validate(run.snapshot_json)

    async def get_run_device_id(self, run_id: str) -> str:
        snapshot = await self.load_snapshot(run_id)
        return snapshot.device.id

    async def start_run(self, run_id: str, event: ExecutionEvent) -> None:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if run is None:
                raise LookupError(f"Run not found: {run_id}")
            run.status = RunStatus.RUNNING.value
            run.started_at = now()
            event_row = await self.events.stage(
                session, event, dedup_key=f"{run_id}:run.started"
            )
            await session.commit()
        if event_row:
            await self.events.publish(event_row)

    async def start_task(
        self,
        run_id: str,
        execution: TaskExecution,
        event: ExecutionEvent,
    ) -> TaskExecution:
        row = TaskRunRow(
            id=execution.id,
            test_run_id=run_id,
            task_index=execution.task_index,
            task_json=execution.task.model_dump(mode="json"),
            status=TaskStatus.RUNNING.value,
            cycle_count=0,
            outcome_json=None,
            started_at=now(),
        )
        async with self.sessions() as session:
            session.add(row)
            event_row = await self.events.stage(
                session,
                event,
                dedup_key=f"{run_id}:{execution.id}:task.started",
            )
            await session.commit()
            await session.refresh(row)
        if event_row:
            await self.events.publish(event_row)
        return self._task_execution(row)

    async def update_cycle(
        self,
        task_run_id: str,
        cycle_count: int,
        event: ExecutionEvent,
    ) -> None:
        async with self.sessions() as session:
            row = await session.get(TaskRunRow, task_run_id)
            if row is None:
                raise LookupError(f"Task run not found: {task_run_id}")
            row.cycle_count = cycle_count
            event_row = await self.events.stage(
                session,
                event,
                dedup_key=(
                    f"{event.run_id}:{task_run_id}:{cycle_count}:cycle.started"
                ),
            )
            await session.commit()
        if event_row:
            await self.events.publish(event_row)

    async def finish_task(
        self,
        task_run_id: str,
        outcome: TaskOutcome,
        event: ExecutionEvent,
    ) -> None:
        async with self.sessions() as session:
            row = await session.get(TaskRunRow, task_run_id)
            if row is None:
                raise LookupError(f"Task run not found: {task_run_id}")
            row.status = outcome.status.value
            row.cycle_count = outcome.cycle_count
            row.outcome_json = outcome.model_dump(mode="json")
            row.finished_at = now()
            event_row = await self.events.stage(
                session,
                event,
                dedup_key=f"{event.run_id}:{task_run_id}:task.finished",
            )
            await session.commit()
        if event_row:
            await self.events.publish(event_row)

    async def skip_remaining(
        self,
        run_id: str,
        executions: list[TaskExecution],
        event: ExecutionEvent,
    ) -> None:
        timestamp = now()
        async with self.sessions() as session:
            for execution in executions:
                if execution.outcome is None:
                    raise ValueError("Skipped execution requires an outcome")
                session.add(
                    TaskRunRow(
                        id=execution.id,
                        test_run_id=run_id,
                        task_index=execution.task_index,
                        task_json=execution.task.model_dump(mode="json"),
                        status=TaskStatus.SKIPPED.value,
                        cycle_count=0,
                        outcome_json=execution.outcome.model_dump(mode="json"),
                        finished_at=timestamp,
                    )
                )
            event_row = await self.events.stage(
                session,
                event,
                dedup_key=f"{run_id}:tasks.skipped",
            )
            await session.commit()
        if event_row:
            await self.events.publish(event_row)

    async def list_task_executions(self, run_id: str) -> list[TaskExecution]:
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TaskRunRow)
                        .where(TaskRunRow.test_run_id == run_id)
                        .order_by(TaskRunRow.task_index)
                    )
                ).all()
            )
        return [self._task_execution(row) for row in rows]

    async def finish_run(
        self,
        run_id: str,
        result: OverallResult,
        event: ExecutionEvent,
        *,
        cancelled: bool = False,
    ) -> None:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if run is None:
                raise LookupError(f"Run not found: {run_id}")
            run.status = (
                RunStatus.CANCELLED.value if cancelled else RunStatus.FINISHED.value
            )
            run.overall_result = result.value
            run.finished_at = now()
            event_row = await self.events.stage(
                session,
                event,
                dedup_key=f"{run_id}:{event.type}",
            )
            await session.commit()
        if event_row:
            await self.events.publish(event_row)

    async def recent_context(
        self, run_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        relevant = {
            "agent.action_selected",
            "tool.finished",
            "task.finished",
        }
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(StepEventRow)
                        .where(
                            StepEventRow.test_run_id == run_id,
                            StepEventRow.type.in_(relevant),
                        )
                        .order_by(StepEventRow.sequence.desc())
                        .limit(limit)
                    )
                ).all()
            )
        return [serialize_event(row) for row in reversed(rows)]

    async def reconcile_orphaned(self) -> list[str]:
        timestamp = now()
        orphan_ids: list[str] = []
        event_rows: list[StepEventRow] = []
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
                            select(TaskRunRow).where(TaskRunRow.test_run_id == run.id)
                        )
                    ).all()
                )
                for task in tasks:
                    if task.status == TaskStatus.RUNNING.value:
                        outcome = TaskOutcome(
                            status=TaskStatus.BLOCKED,
                            reason_code=ReasonCode.PROCESS_RESTARTED,
                            summary="Process restarted before the task completed",
                            cycle_count=task.cycle_count,
                        )
                        task.status = TaskStatus.BLOCKED.value
                        task.outcome_json = outcome.model_dump(mode="json")
                        task.finished_at = timestamp
                error_event = ExecutionErrorEvent(
                    run_id=run.id,
                    reason_code=ReasonCode.PROCESS_RESTARTED,
                    message="Process restarted before execution completed",
                )
                error_row = await self.events.stage(
                    session,
                    error_event,
                    dedup_key=f"{run.id}:process_restarted",
                )
                finished_row = await self.events.stage(
                    session,
                    RunFinishedEvent(
                        run_id=run.id,
                        result=OverallResult.BLOCKED,
                    ),
                    dedup_key=f"{run.id}:run.finished",
                )
                if error_row:
                    event_rows.append(error_row)
                if finished_row:
                    event_rows.append(finished_row)
                orphan_ids.append(run.id)
            await session.commit()
        for event_row in event_rows:
            await self.events.publish(event_row)
        return orphan_ids

    @staticmethod
    def _task_execution(row: TaskRunRow) -> TaskExecution:
        return TaskExecution(
            id=row.id,
            task=Task.model_validate(row.task_json),
            task_index=row.task_index,
            status=TaskStatus(row.status),
            cycle_count=row.cycle_count,
            outcome=(
                TaskOutcome.model_validate(row.outcome_json)
                if row.outcome_json
                else None
            ),
        )
