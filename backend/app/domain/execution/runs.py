"""TestRun/TaskRun 生命周期、快照、结果及确定性聚合规则。"""

from __future__ import annotations

from datetime import datetime
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.ids import TaskRunId, TestPlanId, TestRunId, TestTaskId
from app.domain.planning import TestPlan, TestTask, TestTaskType
from app.domain.resources import LLMProfileSettings, ResourceCatalogSnapshot, ToolCatalogSnapshot


class ExecutionSettings(BaseModel):
    """应用配置及单次运行共同使用的执行预算与交互参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    tool_call_timeout_seconds: float = Field(gt=0, title="工具交互超时", description="外部能力准备及每次工具完整交互的超时秒数。")
    goal_max_failures: int = Field(ge=1, title="Goal 累计失败上限", description="同一运行中每个 Goal 独立累计的失败次数上限。")
    image_history_rounds: int = Field(ge=1, title="图片保留轮数", description="模型上下文保留原图的最近有图片决策轮数。")
    model_call_max_attempts: int = Field(ge=1, le=10, title="模型传输尝试次数", description="单轮模型传输的最多尝试次数，包含首次。")
    model_response_max_attempts: int = Field(ge=1, le=10, title="连续纠错尝试次数", description="连续无效模型响应和工具错误共用的尝试额度。")
    max_cycles_by_task_type: dict[TestTaskType, Annotated[int, Field(ge=1, le=100)]] = Field(
        title="各类任务决策轮次上限", description="每次任务执行独立使用对应类型的上限。",
    )

    @field_validator("max_cycles_by_task_type")
    @classmethod
    def task_types_are_complete(cls, cycle_limits: dict[TestTaskType, int]) -> dict[TestTaskType, int]:
        """执行预算必须覆盖且只覆盖已注册任务类型。"""
        if set(cycle_limits) != set(TestTaskType):
            raise ValueError("max_cycles_by_task_type must contain exactly the registered task types")
        return cycle_limits


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
    GOAL_FAILURE_LIMIT = "goal_failure_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    AGENT_BLOCKED = "agent_blocked"
    TOOL_FAILED = "tool_failed"
    PROCESS_RESTARTED = "process_restarted"
    GLOBAL_FAIL_FAST = "global_fail_fast"
    USER_CANCELLED = "user_cancelled"
    MODEL_CONTEXT_EXCEEDED = "model_context_exceeded"
    UNEXPECTED_ERROR = "unexpected_error"


class TaskRunResult(BaseModel):
    """TaskRun 的终态原因、判断摘要及供后续任务复用的观察记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: ReasonCode
    summary: str = Field(min_length=1)
    memory: str | None = None

    @field_validator("memory")
    @classmethod
    def normalize_optional_memory(cls, value: str | None) -> str | None:
        return value if value and value.strip() else None


class TestRunEnvironmentSnapshot(BaseModel):
    """RunService 在 TestRun 创建时冻结且执行器必须遵守的环境事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_catalog: ToolCatalogSnapshot
    resource_catalog: ResourceCatalogSnapshot
    execution_model: LLMProfileSettings
    execution: ExecutionSettings
    prompt_fingerprints_by_task_type: dict[TestTaskType, Annotated[str, Field(min_length=1)] | None]
    app_version: str = Field(min_length=1)
    execution_protocol_version: Literal["11"]

    @model_validator(mode="after")
    def task_types_are_complete(self) -> "TestRunEnvironmentSnapshot":
        required_task_types = set(TestTaskType)
        for field_name, configured_task_types in (
            ("prompt_fingerprints_by_task_type", self.prompt_fingerprints_by_task_type.keys()),
        ):
            if set(configured_task_types) != required_task_types:
                raise ValueError(f"{field_name} must contain exactly the registered task types")
        return self


class TestRun(BaseModel):
    """一次已确认计划通过 MCP 工具执行的聚合根。"""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TestRunId
    test_plan_id: TestPlanId
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
    """单次计划任务执行或跳过记录；同一任务可重入，sequence 保留运行内顺序。"""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TaskRunId
    test_run_id: TestRunId
    test_task_id: TestTaskId
    sequence: int = Field(ge=1)
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
    environment: TestRunEnvironmentSnapshot
    task_runs: list[TaskRun]


def aggregate_test_run_verdict(task_runs: list[TaskRun], test_tasks: Sequence[TestTask]) -> TestRunVerdict:
    """按产品规则从普通 TaskRun 终态确定性聚合 TestRun verdict。"""

    statuses = [task.status for task in task_runs]
    if TaskRunStatus.CANCELLED in statuses:
        raise ValueError("cancelled TaskRun requires the explicit cancellation path")
    if TaskRunStatus.BLOCKED in statuses or any(
        task.result is not None and task.result.reason_code == ReasonCode.GOAL_FAILURE_LIMIT
        for task in task_runs
    ):
        return TestRunVerdict.BLOCKED
    task_types = {task.test_task_id: task.definition.type for task in test_tasks}
    if any(task.status == TaskRunStatus.FAILED and task_types[task.test_task_id] != TestTaskType.GOAL
           for task in task_runs):
        return TestRunVerdict.FAIL
    if statuses and all(status in {TaskRunStatus.PASSED, TaskRunStatus.FAILED, TaskRunStatus.SKIPPED}
                        for status in statuses):
        return TestRunVerdict.PASS
    return TestRunVerdict.BLOCKED
