from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import ReasonCode
from app.domain.planning import PlanOutput, Task
from app.domain.tools import DeviceCapabilities, ToolDefinition, ToolExecutionResult, ToolInvocation


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class OverallResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TestCaseSnapshot(BaseModel):
    id: str
    name: str
    source_text: str


class PlanRevisionSnapshot(BaseModel):
    id: str
    revision: int
    source: str


class DeviceSnapshot(BaseModel):
    id: str
    health_message: str = ""
    capabilities: DeviceCapabilities | None = None


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_case: TestCaseSnapshot
    plan_revision: PlanRevisionSnapshot
    plan: PlanOutput
    confirmed_assumptions: list[str]
    device: DeviceSnapshot
    model: dict[str, Any]
    enabled_tools: list[ToolDefinition]
    prompt_versions: dict[str, str]
    app_version: str
    execution_protocol_version: str = "2"


class ObservationRef(BaseModel):
    artifact_id: str
    mime_type: str
    activity: str | None = None
    cycle_count: int = Field(ge=1)


class TaskTerminalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "blocked"]
    summary: str = Field(min_length=1)


class ActionSelected(BaseModel):
    type: Literal["action_selected"] = "action_selected"
    invocation: ToolInvocation
    result: ToolExecutionResult | None = None


class TerminalSelected(BaseModel):
    type: Literal["terminal_selected"] = "terminal_selected"
    decision: TaskTerminalDecision
    evidence_artifact_ids: list[str] = Field(default_factory=list)


AgentTurn = Annotated[
    ActionSelected | TerminalSelected,
    Field(discriminator="type"),
]


class TaskOutcome(BaseModel):
    status: TaskStatus
    reason_code: ReasonCode
    summary: str = Field(min_length=1)
    cycle_count: int = Field(ge=0)
    evidence_artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def terminal_status_and_evidence_are_consistent(self) -> "TaskOutcome":
        if self.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            raise ValueError("TaskOutcome requires a terminal task status")
        if (
            self.status in {TaskStatus.PASSED, TaskStatus.FAILED}
            and not self.evidence_artifact_ids
        ):
            raise ValueError("passed/failed outcomes require screenshot evidence")
        return self


class RunOutcome(BaseModel):
    result: OverallResult
    task_counts: dict[str, int]


class TaskExecution(BaseModel):
    id: str
    task: Task
    task_index: int
    status: TaskStatus
    cycle_count: int = 0
    outcome: TaskOutcome | None = None
