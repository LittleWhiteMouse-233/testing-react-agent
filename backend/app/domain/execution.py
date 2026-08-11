from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.device import DeviceEnvironmentSnapshot
from app.domain.errors import ReasonCode
from app.domain.ids import ArtifactId, DeviceId, TaskRunId, TestPlanId, TestRunId, TestTaskId
from app.domain.llm import LLMProfileSnapshot
from app.domain.tools import ToolCatalogSnapshot


class TestRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"


class TestRunVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskRunStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TaskRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: ReasonCode
    summary: str = Field(min_length=1)
    evidence_artifact_ids: list[ArtifactId]

    @model_validator(mode="after")
    def evidence_is_unique(self) -> "TaskRunResult":
        if len(self.evidence_artifact_ids) != len(set(self.evidence_artifact_ids)):
            raise ValueError("evidence_artifact_ids must be unique")
        return self


class TestRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device_environment: DeviceEnvironmentSnapshot
    tool_catalog: ToolCatalogSnapshot
    act_model: LLMProfileSnapshot
    judge_model: LLMProfileSnapshot
    act_prompt_version: str = Field(min_length=1)
    judge_prompt_version: str = Field(min_length=1)
    app_version: str = Field(min_length=1)
    execution_protocol_version: Literal["1"]


class TestRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TestRunId
    test_plan_id: TestPlanId
    device_id: DeviceId
    status: TestRunStatus
    verdict: TestRunVerdict | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> "TestRun":
        if self.status == TestRunStatus.FINISHED:
            if self.verdict is None or self.finished_at is None:
                raise ValueError("finished run requires verdict and finished_at")
        elif self.verdict is not None or self.finished_at is not None:
            raise ValueError("non-finished run cannot have verdict or finished_at")
        return self


class TaskRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TaskRunId
    test_run_id: TestRunId
    test_task_id: TestTaskId
    status: TaskRunStatus
    cycle_count: int = Field(ge=0)
    result: TaskRunResult | None
    started_at: datetime | None
    finished_at: datetime | None

    @model_validator(mode="after")
    def result_is_consistent(self) -> "TaskRun":
        terminal = self.status != TaskRunStatus.RUNNING
        if terminal != (self.result is not None):
            raise ValueError("terminal task run requires exactly one result")
        if terminal and self.finished_at is None:
            raise ValueError("terminal task run requires finished_at")
        if not terminal and self.finished_at is not None:
            raise ValueError("non-terminal task run cannot have finished_at")
        if (
            self.status in {TaskRunStatus.PASSED, TaskRunStatus.FAILED}
            and self.result is not None
            and not self.result.evidence_artifact_ids
        ):
            raise ValueError("passed/failed task run requires screenshot evidence")
        return self


class TaskAgentCompletion(BaseModel):
    """Typed Graph output boundary; not a persisted business entity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskRunStatus
    result: TaskRunResult
    cycle_count: int = Field(ge=0)

    @model_validator(mode="after")
    def status_is_terminal(self) -> "TaskAgentCompletion":
        if self.status == TaskRunStatus.RUNNING:
            raise ValueError("task agent completion requires terminal status")
        return self


def aggregate_test_run_verdict(task_runs: list[TaskRun]) -> TestRunVerdict:
    """The single deterministic TaskRun[] to TestRun verdict conversion."""

    statuses = [task.status for task in task_runs]
    if any(status == TaskRunStatus.FAILED for status in statuses):
        return TestRunVerdict.FAIL
    if any(
        status in {TaskRunStatus.BLOCKED, TaskRunStatus.SKIPPED}
        for status in statuses
    ):
        return TestRunVerdict.BLOCKED
    if statuses and all(status == TaskRunStatus.PASSED for status in statuses):
        return TestRunVerdict.PASS
    return TestRunVerdict.BLOCKED
