"""TestRun/TaskRun 生命周期、快照、结果及确定性聚合规则。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import ArtifactId, DeviceId, TaskRunId, TestPlanId, TestRunId, TestTaskId
from app.domain.planning import TestPlan
from app.domain.resources.device import DeviceEnvironmentSnapshot
from app.domain.resources.llm import LLMProfileSnapshot
from app.domain.resources.tools import ToolCatalogSnapshot


class TestRunStatus(StrEnum):
    """TestRun 的持久化生命周期阶段；取消由终态 verdict 表达。"""

    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"


class TestRunVerdict(StrEnum):
    """整个 TestRun 的确定性结论，而非模型生成的自然语言判断。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskRunStatus(StrEnum):
    """已创建 TaskRun 的生命周期或终态；cancelled 表示启动后被用户终止。"""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ReasonCode(StrEnum):
    """TaskRun/执行错误的稳定结果原因；它是持久化事实而非异常类型。"""

    COMPLETED = "completed"
    ASSERTION_FAILED = "assertion_failed"
    GOAL_UNREACHABLE = "goal_unreachable"
    CYCLE_LIMIT = "cycle_limit"
    DEVICE_UNAVAILABLE = "device_unavailable"
    CAPTURE_FAILED = "capture_failed"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    AGENT_BLOCKED = "agent_blocked"
    TOOL_FAILED = "tool_failed"
    PROCESS_RESTARTED = "process_restarted"
    GLOBAL_FAIL_FAST = "global_fail_fast"
    USER_CANCELLED = "user_cancelled"
    PROMPT_VERSION_MISMATCH = "prompt_version_mismatch"
    MODEL_PROFILE_MISMATCH = "model_profile_mismatch"
    MODEL_CONTEXT_EXCEEDED = "model_context_exceeded"
    UNEXPECTED_ERROR = "unexpected_error"


class TaskRunResult(BaseModel):
    """TaskRun 的终态原因、摘要与证据引用；状态和 cycle 不在此重复。"""

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
    """RunService 在 TestRun 创建时冻结且执行器必须遵守的环境事实。"""

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
    """一次已确认计划在单台设备上的执行聚合根。"""

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
    """真正启动或由 fail-fast 跳过的单个计划任务执行实体。"""

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
        if self.result is not None:
            is_user_cancelled = (
                self.result.reason_code == ReasonCode.USER_CANCELLED
            )
            if (self.status == TaskRunStatus.CANCELLED) != is_user_cancelled:
                raise ValueError(
                    "cancelled task run and USER_CANCELLED reason must occur together"
                )
        return self


class TaskAgentCompletion(BaseModel):
    """TaskAgent 的 typed 输出边界；执行器随后把它提交为 TaskRun 终态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskRunStatus
    result: TaskRunResult
    cycle_count: int = Field(ge=0)

    @model_validator(mode="after")
    def status_is_terminal(self) -> "TaskAgentCompletion":
        if self.status == TaskRunStatus.RUNNING:
            raise ValueError("task agent completion requires terminal status")
        is_user_cancelled = self.result.reason_code == ReasonCode.USER_CANCELLED
        if (self.status == TaskRunStatus.CANCELLED) != is_user_cancelled:
            raise ValueError(
                "cancelled completion and USER_CANCELLED reason must occur together"
            )
        return self


class TestRunDetail(BaseModel):
    """执行信息家族的完整查询组合，由 repository 装配并供执行/API 消费。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: TestRun
    test_plan: TestPlan
    snapshot: TestRunSnapshot
    task_runs: list[TaskRun]


def aggregate_test_run_verdict(task_runs: list[TaskRun]) -> TestRunVerdict:
    """按产品规则从普通 TaskRun 终态确定性聚合 TestRun verdict。"""

    statuses = [task.status for task in task_runs]
    if TaskRunStatus.CANCELLED in statuses:
        raise ValueError("cancelled TaskRun requires the explicit cancellation path")
    if TaskRunStatus.FAILED in statuses:
        return TestRunVerdict.FAIL
    if any(
        status in {TaskRunStatus.BLOCKED, TaskRunStatus.SKIPPED}
        for status in statuses
    ):
        return TestRunVerdict.BLOCKED
    if statuses and all(status == TaskRunStatus.PASSED for status in statuses):
        return TestRunVerdict.PASS
    return TestRunVerdict.BLOCKED
