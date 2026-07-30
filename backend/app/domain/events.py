from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.errors import ReasonCode
from app.domain.execution import ObservationRef, OverallResult, TaskOutcome
from app.domain.planning import Task
from app.domain.tools import ToolExecutionResult, ToolInvocation


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_run_id: str | None = None


class RunStartedEvent(EventBase):
    type: Literal["run.started"] = "run.started"
    device_id: str


class TaskStartedEvent(EventBase):
    type: Literal["task.started"] = "task.started"
    task_index: int
    task: Task


class CycleStartedEvent(EventBase):
    type: Literal["cycle.started"] = "cycle.started"
    cycle_count: int


class ObservationCapturedEvent(EventBase):
    type: Literal["observation.captured"] = "observation.captured"
    observation: ObservationRef


class AgentActionSelectedEvent(EventBase):
    type: Literal["agent.action_selected"] = "agent.action_selected"
    cycle_count: int
    invocation: ToolInvocation


class AgentTerminalSelectedEvent(EventBase):
    type: Literal["agent.terminal_selected"] = "agent.terminal_selected"
    cycle_count: int
    status: Literal["passed", "failed", "blocked"]
    summary: str
    evidence_artifact_ids: list[str]


class ToolStartedEvent(EventBase):
    type: Literal["tool.started"] = "tool.started"
    cycle_count: int
    invocation: ToolInvocation


class ToolFinishedEvent(EventBase):
    type: Literal["tool.finished"] = "tool.finished"
    cycle_count: int
    invocation: ToolInvocation
    result: ToolExecutionResult


class AgentResponseInvalidEvent(EventBase):
    type: Literal["agent.response_invalid"] = "agent.response_invalid"
    cycle_count: int
    attempt: int
    message: str


class ExecutionErrorEvent(EventBase):
    type: Literal["execution.error"] = "execution.error"
    reason_code: ReasonCode
    message: str


class TaskFinishedEvent(EventBase):
    type: Literal["task.finished"] = "task.finished"
    outcome: TaskOutcome


class TasksSkippedEvent(EventBase):
    type: Literal["tasks.skipped"] = "tasks.skipped"
    task_ids: list[str]
    reason_code: ReasonCode = ReasonCode.GLOBAL_FAIL_FAST


class RunFinishedEvent(EventBase):
    type: Literal["run.finished"] = "run.finished"
    result: OverallResult


class RunCancelledEvent(EventBase):
    type: Literal["run.cancelled"] = "run.cancelled"
    result: Literal[OverallResult.CANCELLED] = OverallResult.CANCELLED


ExecutionEvent = Annotated[
    RunStartedEvent
    | TaskStartedEvent
    | CycleStartedEvent
    | ObservationCapturedEvent
    | AgentActionSelectedEvent
    | AgentTerminalSelectedEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | AgentResponseInvalidEvent
    | ExecutionErrorEvent
    | TaskFinishedEvent
    | TasksSkippedEvent
    | RunFinishedEvent
    | RunCancelledEvent,
    Field(discriminator="type"),
]
EXECUTION_EVENT_ADAPTER = TypeAdapter(ExecutionEvent)


class StoredExecutionEvent(BaseModel):
    id: str
    sequence: int
    timestamp: datetime
    event: ExecutionEvent

    def to_api_dict(self) -> dict[str, Any]:
        data = self.event.model_dump(mode="json")
        return {
            "id": self.id,
            "sequence": self.sequence,
            "run_id": self.event.run_id,
            "task_run_id": self.event.task_run_id,
            "type": self.event.type,
            "timestamp": self.timestamp.isoformat(),
            "payload": {
                key: value
                for key, value in data.items()
                if key not in {"run_id", "task_run_id", "type"}
            },
        }
