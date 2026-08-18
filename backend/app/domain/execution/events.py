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
    """TaskAgent 开始一个新观察 cycle 的计数事实。"""

    type: Literal["cycle.started"] = "cycle.started"
    cycle_count: int = Field(ge=1)


class ToolStartedEvent(RunEventBase):
    """工具通过取消安全边界并即将执行的事实。"""

    type: Literal["tool.started"] = "tool.started"
    call_id: ToolCallId


class ExecutionErrorEvent(RunEventBase):
    """阻塞执行的基础设施或协议错误。"""

    type: Literal["execution.error"] = "execution.error"
    reason_code: ReasonCode
    message: str = Field(min_length=1)


class TasksSkippedEvent(RunEventBase):
    """全局 fail-fast 批量创建未执行 TaskRun 的事实。"""

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
    """事件存储分配的 ID、顺序和时间与 canonical 事件的统一 envelope。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: RunEventId
    sequence: int = Field(ge=1)
    occurred_at: datetime
    event: RunEvent
