from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from app.domain.execution import (
    ModelSnapshot,
    ObservationRef,
    OverallResult,
    RunSnapshot,
    RunStatus,
    TaskOutcome,
    TaskStatus,
)
from app.domain.errors import ReasonCode
from app.domain.planning import DeviceProfile, PlanOutput, Task
from app.domain.tools import (
    DeviceCapabilities,
    DeviceHealth,
    ToolExecutionResult,
    ToolInvocation,
)

T = TypeVar("T")


class TestCaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1)


class PlanCreate(BaseModel):
    device_profile: DeviceProfile | None = None


class PlanRevisionCreate(BaseModel):
    plan: PlanOutput


class RunCreate(BaseModel):
    plan_revision_id: str
    device_id: str
    confirmed_assumptions: list[str] = Field(default_factory=list)


class ExportCreate(BaseModel):
    format: Literal["json", "html"]


class TestCaseResponse(BaseModel):
    id: str
    name: str
    source_text: str
    created_at: str
    updated_at: str


class PlanRevisionResponse(BaseModel):
    id: str
    test_case_id: str
    revision: int
    source: str
    parent_revision_id: str | None = None
    plan: PlanOutput
    model_info: ModelSnapshot
    created_at: str


class TaskExecutionResponse(BaseModel):
    id: str
    task: Task
    task_index: int
    status: TaskStatus
    cycle_count: int
    outcome: TaskOutcome | None = None


class RunResponse(BaseModel):
    id: str
    test_case_id: str
    plan_revision_id: str
    device_id: str
    status: RunStatus
    overall_result: OverallResult | None = None
    snapshot: RunSnapshot
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str


class RunDetailResponse(RunResponse):
    task_runs: list[TaskExecutionResponse]


class PageResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int


class StepEventBase(BaseModel):
    id: str
    sequence: int
    run_id: str
    task_run_id: str | None = None
    timestamp: str


class RunStartedPayload(BaseModel):
    device_id: str


class TaskStartedPayload(BaseModel):
    task_index: int
    task: Task


class CycleStartedPayload(BaseModel):
    cycle_count: int


class ObservationCapturedPayload(BaseModel):
    observation: ObservationRef


class AgentActionSelectedPayload(BaseModel):
    cycle_count: int
    invocation: ToolInvocation


class AgentTerminalSelectedPayload(BaseModel):
    cycle_count: int
    status: Literal["passed", "failed", "blocked"]
    summary: str
    evidence_artifact_ids: list[str]


class ToolStartedPayload(BaseModel):
    cycle_count: int
    invocation: ToolInvocation


class ToolFinishedPayload(ToolStartedPayload):
    result: ToolExecutionResult


class AgentResponseInvalidPayload(BaseModel):
    cycle_count: int
    attempt: int
    message: str


class ExecutionErrorPayload(BaseModel):
    reason_code: ReasonCode
    message: str


class TaskFinishedPayload(BaseModel):
    outcome: TaskOutcome


class TasksSkippedPayload(BaseModel):
    task_ids: list[str]
    reason_code: ReasonCode


class RunFinishedPayload(BaseModel):
    result: OverallResult


class RunStartedStepEvent(StepEventBase):
    type: Literal["run.started"]
    payload: RunStartedPayload


class TaskStartedStepEvent(StepEventBase):
    type: Literal["task.started"]
    payload: TaskStartedPayload


class CycleStartedStepEvent(StepEventBase):
    type: Literal["cycle.started"]
    payload: CycleStartedPayload


class ObservationCapturedStepEvent(StepEventBase):
    type: Literal["observation.captured"]
    payload: ObservationCapturedPayload


class AgentActionSelectedStepEvent(StepEventBase):
    type: Literal["agent.action_selected"]
    payload: AgentActionSelectedPayload


class AgentTerminalSelectedStepEvent(StepEventBase):
    type: Literal["agent.terminal_selected"]
    payload: AgentTerminalSelectedPayload


class ToolStartedStepEvent(StepEventBase):
    type: Literal["tool.started"]
    payload: ToolStartedPayload


class ToolFinishedStepEvent(StepEventBase):
    type: Literal["tool.finished"]
    payload: ToolFinishedPayload


class AgentResponseInvalidStepEvent(StepEventBase):
    type: Literal["agent.response_invalid"]
    payload: AgentResponseInvalidPayload


class ExecutionErrorStepEvent(StepEventBase):
    type: Literal["execution.error"]
    payload: ExecutionErrorPayload


class TaskFinishedStepEvent(StepEventBase):
    type: Literal["task.finished"]
    payload: TaskFinishedPayload


class TasksSkippedStepEvent(StepEventBase):
    type: Literal["tasks.skipped"]
    payload: TasksSkippedPayload


class RunFinishedStepEvent(StepEventBase):
    type: Literal["run.finished"]
    payload: RunFinishedPayload


class RunCancelledStepEvent(StepEventBase):
    type: Literal["run.cancelled"]
    payload: RunFinishedPayload


StepEventResponse = Annotated[
    RunStartedStepEvent
    | TaskStartedStepEvent
    | CycleStartedStepEvent
    | ObservationCapturedStepEvent
    | AgentActionSelectedStepEvent
    | AgentTerminalSelectedStepEvent
    | ToolStartedStepEvent
    | ToolFinishedStepEvent
    | AgentResponseInvalidStepEvent
    | ExecutionErrorStepEvent
    | TaskFinishedStepEvent
    | TasksSkippedStepEvent
    | RunFinishedStepEvent
    | RunCancelledStepEvent,
    Field(discriminator="type"),
]


class ArtifactResponse(BaseModel):
    id: str
    run_id: str
    task_run_id: str | None = None
    type: str
    mime_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    created_at: str
    url: str


class ReportRunResponse(BaseModel):
    id: str
    status: RunStatus
    overall_result: OverallResult | None = None
    device_id: str
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str


class ReportSummaryResponse(BaseModel):
    task_count: int
    passed: int
    failed: int
    blocked: int
    skipped: int
    event_count: int
    artifact_count: int


class ReportTaskResponse(BaseModel):
    id: str
    task_index: int
    definition: Task
    status: TaskStatus
    cycle_count: int
    outcome: TaskOutcome | None = None
    started_at: str | None = None
    finished_at: str | None = None
    artifacts: list[ArtifactResponse]
    evidence: list[ArtifactResponse]


class ReportResponse(BaseModel):
    run: ReportRunResponse
    snapshot: RunSnapshot
    summary: ReportSummaryResponse
    tasks: list[ReportTaskResponse]
    events: list[StepEventResponse]
    artifacts: list[ArtifactResponse]


class DeviceSummaryResponse(BaseModel):
    id: str
    type: str
    health: DeviceHealth


class DeviceHealthResponse(BaseModel):
    id: str
    health: DeviceHealth
    capabilities: DeviceCapabilities | None = None


class CancelResponse(BaseModel):
    run_id: str
    cancel_requested: bool
