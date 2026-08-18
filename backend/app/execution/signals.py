from __future__ import annotations

"""Execution Graph 向持久事件流水线发送的窄化 custom stream 信号。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.ids import MessageId, ToolCallId


class MessageValidationSignal(BaseModel):
    """模型消息校验失败的执行内信号，由 RunExecutor 投影为事件。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["message.validation_failed"] = "message.validation_failed"
    message_id: MessageId
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1)


class ToolStartedSignal(BaseModel):
    """工具通过取消边界后的执行内信号，由 RunExecutor 投影为事件。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool.started"] = "tool.started"
    call_id: ToolCallId


GraphSignal = MessageValidationSignal | ToolStartedSignal
GRAPH_SIGNAL_ADAPTER = TypeAdapter(GraphSignal)
