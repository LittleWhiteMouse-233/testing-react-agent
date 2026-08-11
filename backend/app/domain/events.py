from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.errors import ReasonCode
from app.domain.ids import MessageId, RunEventId, TaskRunId, TestRunId, ToolCallId
from app.domain.messages import RunMessage


class RunEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_run_id: TestRunId
    task_run_id: TaskRunId | None


class MessageAppendedEvent(RunEventBase):
    type: Literal["message.appended"] = "message.appended"
    message: RunMessage


class MessageValidationFailedEvent(RunEventBase):
    type: Literal["message.validation_failed"] = "message.validation_failed"
    message_id: MessageId
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1)


class RunLifecycleEvent(RunEventBase):
    type: Literal["run.started", "run.finished", "run.cancelled"]


class TaskLifecycleEvent(RunEventBase):
    type: Literal["task.started", "task.finished"]


class CycleStartedEvent(RunEventBase):
    type: Literal["cycle.started"] = "cycle.started"
    cycle_count: int = Field(ge=1)


class ToolStartedEvent(RunEventBase):
    type: Literal["tool.started"] = "tool.started"
    call_id: ToolCallId


class ExecutionErrorEvent(RunEventBase):
    type: Literal["execution.error"] = "execution.error"
    reason_code: ReasonCode
    message: str = Field(min_length=1)


class TasksSkippedEvent(RunEventBase):
    type: Literal["tasks.skipped"] = "tasks.skipped"
    task_run_ids: list[TaskRunId]


RunEvent = Annotated[
    MessageAppendedEvent
    | MessageValidationFailedEvent
    | RunLifecycleEvent
    | TaskLifecycleEvent
    | CycleStartedEvent
    | ToolStartedEvent
    | ExecutionErrorEvent
    | TasksSkippedEvent,
    Field(discriminator="type"),
]
RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)


class StoredRunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: RunEventId
    sequence: int = Field(ge=1)
    occurred_at: datetime
    event: RunEvent
