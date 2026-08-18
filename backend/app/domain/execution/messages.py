"""LangChain Message 面向 REST、SSE 与报告的稳定公开投影结构。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ids import ArtifactId, MessageId, ToolCallId


class RunTextBlock(BaseModel):
    """公开消息中的纯文本内容块。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["text"] = "text"
    text: str


class RunImageArtifactBlock(BaseModel):
    """公开消息中的截图证据引用；不会复制图片 base64。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["image_artifact"] = "image_artifact"
    artifact_id: ArtifactId


RunContentBlock = Annotated[
    RunTextBlock | RunImageArtifactBlock,
    Field(discriminator="type"),
]


class RunToolCall(BaseModel):
    """模型发出的一个已解析工具调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: ToolCallId
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class RunInvalidToolCall(BaseModel):
    """LangChain 已定型 InvalidToolCall 的稳定公开字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: ToolCallId | None
    name: str | None
    arguments: str | None
    error: str | None


class RunMessageBase(BaseModel):
    """所有公开 Run Message 共享的身份与内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: MessageId
    content: list[RunContentBlock]


class RunSystemMessage(RunMessageBase):
    """TaskAgent 实际使用的系统指令公开投影。"""

    role: Literal["system"] = "system"


class RunHumanMessage(RunMessageBase):
    """用户/运行时上下文和截图观察的公开投影。"""

    role: Literal["human"] = "human"


class RunAIMessage(RunMessageBase):
    """模型回复及其工具调用事实的公开投影。"""

    role: Literal["ai"] = "ai"
    tool_calls: list[RunToolCall]
    invalid_tool_calls: list[RunInvalidToolCall]


class RunToolMessage(RunMessageBase):
    """框架工具执行结果的公开投影。"""

    role: Literal["tool"] = "tool"
    tool_call_id: ToolCallId
    name: str | None
    status: Literal["success", "error"]


RunMessage = Annotated[
    RunSystemMessage | RunHumanMessage | RunAIMessage | RunToolMessage,
    Field(discriminator="role"),
]
