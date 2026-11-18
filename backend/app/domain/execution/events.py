"""TestRun 持久事件及统一 StoredRunEvent envelope。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.execution.messages import RunMessage
from app.domain.execution.runs import ReasonCode
from app.domain.ids import MessageId, RunEventId, TaskRunId, TestRunId, ToolCallId


class RunEventBase(BaseModel):
    """所有执行事件共享的 TestRun 与可选 TaskRun 归属。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    test_run_id: TestRunId
    task_run_id: TaskRunId | None


class MessageAppendedEvent(RunEventBase):
    """Graph Message 首次成为公开、持久事实。"""

    type: Literal["message.appended"] = "message.appended"
    message: RunMessage


class MessageValidationFailedEvent(RunEventBase):
    """模型消息未通过单工具调用协议校验。"""

    type: Literal["message.validation_failed"] = "message.validation_failed"
    message_id: MessageId
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1)


class RunLifecycleEvent(RunEventBase):
    """TestRun 生命周期改变的通知事实，不复制实体字段。"""

    type: Literal["run.started", "run.finished", "run.cancelled"]


class TaskLifecycleEvent(RunEventBase):
    """TaskRun 启动或结束的通知事实。"""

    type: Literal["task.started", "task.finished"]


class CycleStartedEvent(RunEventBase):
    """TaskAgent 在首次模型请求前开始逻辑决策轮；开始即计轮，传输重试不另计。"""

    type: Literal["cycle.started"] = "cycle.started"
    cycle_count: int = Field(ge=1)


class ContextTokens(BaseModel):
    """Estimated request tokens partitioned into disjoint display categories."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    system_prompt: int = Field(default=0, ge=0)
    tool_definitions: int = Field(default=0, ge=0)
    user_messages: int = Field(default=0, ge=0)
    skill_body: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tool_return_text: int = Field(default=0, ge=0)
    tool_return_nontext: int = Field(default=0, ge=0)
    assistant_messages: int = Field(default=0, ge=0)


class ModelContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_budget_tokens: int = Field(gt=0)
    tokens: ContextTokens
    active_resources: dict[str, list[str]]  # Canonical URIs grouped by source.


class ContextUpdatedEvent(RunEventBase):
    type: Literal["context.updated"] = "context.updated"
    context: ModelContextSnapshot


class NodeExecutionEvent(RunEventBase):
    type: Literal["node.execution"] = "node.execution"
    node_name: str
    started_at: datetime
    finished_at: datetime


class ToolCallEvent(RunEventBase):
    """发起、撤回或成功返回一次工具调用。"""

    type: Literal["tool.started", "tool.succeeded", "tool.withdrawn"]
    tool_name: str
    call_id: ToolCallId


class ExecutionErrorEvent(RunEventBase):
    """阻塞执行的基础设施或协议错误。"""

    type: Literal["execution.error"] = "execution.error"
    reason_code: ReasonCode
    message: str = Field(min_length=1)


class TasksSkippedEvent(RunEventBase):
    """执行停止后，按任务终止策略批量创建后缀 skipped 记录的事实。"""

    type: Literal["tasks.skipped"] = "tasks.skipped"
    task_run_ids: list[TaskRunId]


RunEvent = Annotated[
    MessageAppendedEvent
    | MessageValidationFailedEvent
    | RunLifecycleEvent
    | TaskLifecycleEvent
    | CycleStartedEvent
    | ToolCallEvent
    | ContextUpdatedEvent
    | NodeExecutionEvent
    | ExecutionErrorEvent
    | TasksSkippedEvent,
    Field(discriminator="type"),
]
RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)


class StoredRunEvent(BaseModel):
    """事件存储分配的 ID、顺序和时间与 canonical 事件的统一 envelope。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: RunEventId
    sequence: int = Field(ge=1)
    occurred_at: datetime
    event: RunEvent
