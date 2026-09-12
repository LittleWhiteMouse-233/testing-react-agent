"""LangChain Message 到稳定公开 RunMessage 的唯一投影边界。"""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.domain.execution import (
    RunAIMessage,
    RunContentBlock,
    RunHumanMessage,
    RunImageArtifactBlock,
    RunInvalidToolCall,
    RunMessage,
    RunSystemMessage,
    RunTextBlock,
    RunToolCall,
    RunToolMessage,
)


def project_run_message(message: BaseMessage) -> RunMessage:
    """The only LangChain message to stable public message conversion.

    Image data is deliberately never dumped. Its standard block id is the
    already-persisted Artifact id. Expired image placeholders project to the
    same public image reference, so State retention never changes history.
    """

    if message.id is None:
        raise ValueError("public messages require an id assigned by add_messages")
    content = _project_content(message)
    common = {"message_id": message.id, "content": content}
    if isinstance(message, SystemMessage):
        return RunSystemMessage(**common)
    if isinstance(message, HumanMessage):
        return RunHumanMessage(**common)
    if isinstance(message, AIMessage):
        return RunAIMessage(
            **common,
            tool_calls=[
                RunToolCall(
                    id=_required_call_id(call.get("id")),
                    name=call["name"],
                    arguments=dict(call.get("args") or {}),
                )
                for call in message.tool_calls
            ],
            invalid_tool_calls=[
                RunInvalidToolCall(
                    id=call.get("id"),
                    name=call.get("name"),
                    arguments=call.get("args"),
                    error=call.get("error"),
                )
                for call in message.invalid_tool_calls
            ],
        )
    if isinstance(message, ToolMessage):
        return RunToolMessage(
            **common,
            tool_call_id=message.tool_call_id,
            name=message.name,
            status=message.status,
        )
    raise TypeError(f"unsupported LangChain message: {type(message).__name__}")


def _required_call_id(value: str | None) -> str:
    if not value:
        raise ValueError("public AI tool calls require an id")
    return value


def _project_content(message: BaseMessage) -> list[RunContentBlock]:
    blocks: list[RunContentBlock] = []
    for block in message.content_blocks:
        block_type = block.get("type")
        if block_type == "text":
            artifact_id = (block.get("extras") or {}).get("artifact_id")
            if artifact_id is not None:
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise ValueError("public screenshot placeholders require an Artifact id")
                blocks.append(RunImageArtifactBlock(artifact_id=artifact_id))
            else:
                blocks.append(RunTextBlock(text=str(block.get("text", ""))))
        elif block_type == "image":
            artifact_id = block.get("id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("public image content requires an Artifact id")
            blocks.append(RunImageArtifactBlock(artifact_id=artifact_id))
        elif block_type in {"tool_call", "invalid_tool_call"}:
            # AI tool calls have a dedicated typed field and must not be copied.
            continue
        else:
            raise ValueError(f"unsupported public content block: {block_type!r}")
    return blocks
