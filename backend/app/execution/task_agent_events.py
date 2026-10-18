from __future__ import annotations

from dataclasses import dataclass

from app.domain.execution import (
    CycleStartedEvent,
    MessageValidationFailedEvent,
    ToolStartedEvent,
)
from app.domain.ids import MessageId, TaskRunId, TestRunId, ToolCallId
from app.persistence.run_repository import SqlAlchemyRunRepository


@dataclass(frozen=True)
class TaskAgentEventRecorder:
    """Record TaskAgent facts with one TaskRun's immutable event context."""

    repository: SqlAlchemyRunRepository
    test_run_id: TestRunId
    task_run_id: TaskRunId

    async def record_cycle_started(self, cycle_count: int) -> None:
        await self.repository.start_cycle(
            CycleStartedEvent(
                test_run_id=self.test_run_id, task_run_id=self.task_run_id,
                cycle_count=cycle_count,
            ),
            dedup_key=f"{self.test_run_id}:{self.task_run_id}:{cycle_count}:cycle.started",
        )

    async def record_message_validation_failed(self, message_id: MessageId, attempt: int, reason: str) -> None:
        await self.repository.append_event(
            MessageValidationFailedEvent(
                test_run_id=self.test_run_id, task_run_id=self.task_run_id,
                message_id=message_id, attempt=attempt, reason=reason,
            ),
            dedup_key=f"{self.test_run_id}:{self.task_run_id}:validation:{message_id}:{attempt}",
        )

    async def record_tool_started(self, call_id: ToolCallId) -> None:
        await self.repository.append_event(
            ToolStartedEvent(test_run_id=self.test_run_id, task_run_id=self.task_run_id, call_id=call_id),
            dedup_key=f"{self.test_run_id}:{self.task_run_id}:tool.started:{call_id}",
        )
