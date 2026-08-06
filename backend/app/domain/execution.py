from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import ReasonCode
from app.domain.planning import PlanOutput, Task
from app.domain.tools import (
    DeviceCapabilitiesSnapshot,
    DeviceDescription,
    DeviceHealth,
)


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
    health: DeviceHealth
    description: DeviceDescription | None = None
    capabilities: DeviceCapabilitiesSnapshot | None = None


class ModelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str | None = None
    temperature: float
    timeout_seconds: float = Field(gt=0)


class RunModelsSnapshot(BaseModel):
    planning: ModelSnapshot
    act: ModelSnapshot
    judge: ModelSnapshot


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_case: TestCaseSnapshot
    plan_revision: PlanRevisionSnapshot
    plan: PlanOutput
    confirmed_assumptions: list[str]
    device: DeviceSnapshot
    models: RunModelsSnapshot
    enabled_tool_names: list[str]
    prompt_versions: dict[str, str]
    app_version: str
    execution_protocol_version: str = "1"


class ObservationRef(BaseModel):
    artifact_id: str
    mime_type: str
    activity: str | None = None
    cycle_count: int = Field(ge=1)


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
