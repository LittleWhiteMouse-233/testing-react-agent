from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.ids import MessageId, ToolCallId


class MessageValidationSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["message.validation_failed"] = "message.validation_failed"
    message_id: MessageId
    attempt: int = Field(ge=1)
    reason: str = Field(min_length=1)


class ToolStartedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool.started"] = "tool.started"
    call_id: ToolCallId


GraphSignal = MessageValidationSignal | ToolStartedSignal
GRAPH_SIGNAL_ADAPTER = TypeAdapter(GraphSignal)
