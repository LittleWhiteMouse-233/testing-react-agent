from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ids import ArtifactId, MessageId, ToolCallId


class RunTextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    text: str


class RunImageArtifactBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["image_artifact"] = "image_artifact"
    artifact_id: ArtifactId


RunContentBlock = Annotated[
    RunTextBlock | RunImageArtifactBlock,
    Field(discriminator="type"),
]


class RunToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ToolCallId
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class RunInvalidToolCall(BaseModel):
    """Stable public fields from LangChain's finalized InvalidToolCall."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ToolCallId | None
    name: str | None
    arguments: str | None
    error: str | None


class RunMessageBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: MessageId
    content: list[RunContentBlock]


class RunSystemMessage(RunMessageBase):
    role: Literal["system"] = "system"


class RunHumanMessage(RunMessageBase):
    role: Literal["human"] = "human"


class RunAIMessage(RunMessageBase):
    role: Literal["ai"] = "ai"
    tool_calls: list[RunToolCall]
    invalid_tool_calls: list[RunInvalidToolCall]


class RunToolMessage(RunMessageBase):
    role: Literal["tool"] = "tool"
    tool_call_id: ToolCallId
    name: str | None
    status: Literal["success", "error"]


RunMessage = Annotated[
    RunSystemMessage | RunHumanMessage | RunAIMessage | RunToolMessage,
    Field(discriminator="role"),
]
