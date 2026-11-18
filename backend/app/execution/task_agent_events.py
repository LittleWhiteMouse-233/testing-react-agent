from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from app.domain.execution import (
    CycleStartedEvent,
    MessageValidationFailedEvent,
    ContextUpdatedEvent, ModelContextSnapshot, NodeExecutionEvent, ToolCallEvent,
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

    async def record_tool_call(self, phase: Literal["tool.started", "tool.succeeded", "tool.withdrawn"],
                               call_id: ToolCallId, tool_name: str, cycle_count: int) -> None:
        await self.repository.append_event(
            ToolCallEvent(test_run_id=self.test_run_id, task_run_id=self.task_run_id,
                          type=phase, call_id=call_id, tool_name=tool_name),
            dedup_key=f"{self.test_run_id}:{self.task_run_id}:{cycle_count}:{phase}:{call_id}",
        )

    async def record_context(self, context: ModelContextSnapshot) -> None:
        await self.repository.append_event(ContextUpdatedEvent(
            test_run_id=self.test_run_id, task_run_id=self.task_run_id, context=context,
        ))


class TaskNodeTiming(AsyncCallbackHandler):
    """Collect direct graph children only. Nested chains share node metadata."""
    raise_error = True
    run_inline = True

    def __init__(self, recorder: TaskAgentEventRecorder) -> None:
        self.recorder = recorder
        self.graph_run_id: UUID | None = None
        self.starts: dict[UUID, tuple[str, datetime]] = {}

    async def on_chain_start(self, serialized: dict[str, Any] | None, inputs: Any, *,
                             run_id: UUID, parent_run_id: UUID | None = None,
                             metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if self.graph_run_id is None:
            self.graph_run_id = run_id
        elif parent_run_id == self.graph_run_id and metadata and (node := metadata.get("langgraph_node")):
            self.starts[run_id] = (node, datetime.now(UTC))

    async def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        await self._finish(run_id)

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        await self._finish(run_id)

    async def _finish(self, run_id: UUID) -> None:
        started = self.starts.pop(run_id, None)
        if started is not None:
            node_name, started_at = started
            await self.recorder.repository.append_event(NodeExecutionEvent(
                test_run_id=self.recorder.test_run_id, task_run_id=self.recorder.task_run_id,
                node_name=node_name, started_at=started_at, finished_at=datetime.now(UTC),
            ))
