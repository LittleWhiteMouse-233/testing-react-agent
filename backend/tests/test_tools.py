from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
from langgraph.graph import END, START, MessagesState, StateGraph
from langsmith import tracing_context

from app.device.test_fake import FakeDeviceController
from app.domain.errors import TransientToolError
from app.domain.planning import TaskType
from app.domain.tools import ToolExecutionResult, ToolExecutionStatus
from app.services.tools import FrameworkToolProvider, ToolBuildRequest


async def _build(
    provider: FrameworkToolProvider,
    activity: TaskType,
):
    device = FakeDeviceController()
    return await provider.build_tools(
        ToolBuildRequest(
            activity=activity,
            device=device,
            capabilities=await device.capabilities(),
        )
    )


async def _invoke(tool_set, name: str, args: dict[str, object]) -> ToolExecutionResult:
    node = ToolNode(
        tool_set.tools,
        handle_tool_errors=False,
        awrap_tool_call=tool_set.awrap_tool_call,
    )
    graph = StateGraph(MessagesState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    with tracing_context(enabled=False):
        output = await graph.compile().ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="invoke",
                        tool_calls=[
                            {
                                "name": name,
                                "args": args,
                                "id": "test-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        )
    message = output["messages"][-1]
    assert isinstance(message, ToolMessage)
    return ToolExecutionResult.model_validate(message.artifact)


@pytest.mark.asyncio
async def test_provider_preserves_schema_and_filters_explicit_scopes() -> None:
    provider = FrameworkToolProvider()

    @tool
    async def act_only(value: str) -> str:
        """Mutate a setting."""
        return value

    @tool
    async def read_only(value: str) -> str:
        """Read a setting."""
        return value

    read_only.metadata = {"atv.scopes": ["act", "judge"]}
    provider.register(act_only)
    provider.register(read_only)

    act = await _build(provider, TaskType.ACT)
    judge = await _build(provider, TaskType.JUDGE)

    assert {"device_press_key", "device_input_text", "device_wait"} <= act.names
    assert {"act_only", "read_only"} <= act.names
    assert judge.names == {"device_wait", "read_only"}
    schema = next(tool for tool in act.tools if tool.name == "act_only").args_schema
    assert schema is not None
    assert "value" in schema.model_json_schema()["required"]


@pytest.mark.asyncio
async def test_provider_times_out_without_replaying_unknown_result() -> None:
    provider = FrameworkToolProvider(action_timeout_seconds=0.01)
    attempts = 0

    @tool
    async def slow_tool() -> str:
        """Run a slow operation."""
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.1)
        return "done"

    provider.register(slow_tool)
    tool_set = await _build(provider, TaskType.ACT)
    result = await _invoke(tool_set, "slow_tool", {})

    assert result.status == ToolExecutionStatus.TIMED_OUT
    assert attempts == 1


@pytest.mark.asyncio
async def test_provider_retries_only_transient_tool_errors() -> None:
    provider = FrameworkToolProvider()
    attempts = 0

    @tool
    async def flaky_tool() -> str:
        """Run an operation with transient failures."""
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientToolError("retry")
        return "recovered"

    provider.register(flaky_tool)
    tool_set = await _build(provider, TaskType.ACT)
    result = await _invoke(tool_set, "flaky_tool", {})

    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert result.summary == "recovered"
    assert attempts == 3


def test_provider_rejects_duplicate_and_reserved_tool_names() -> None:
    provider = FrameworkToolProvider()

    @tool("external")
    def first() -> str:
        """First tool."""
        return "first"

    @tool("external")
    def duplicate() -> str:
        """Duplicate tool."""
        return "duplicate"

    @tool("finish_task")
    def reserved() -> str:
        """Reserved tool."""
        return "reserved"

    provider.register(first)
    with pytest.raises(ValueError, match="Duplicate"):
        provider.register(duplicate)
    with pytest.raises(ValueError, match="reserved"):
        provider.register(reserved)
