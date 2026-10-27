from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langsmith import tracing_context

from app.resources.mcp import MCPClientProvider
from app.resources.tools import ToolProvider, snapshot_tools
from mcp_support import image_block, load_mcp_client_provider, read_records, replay_call, text_block, write_mcp_config


@pytest.mark.asyncio
async def test_replay_scenario_continues_across_framework_sessions(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [
        replay_call(content=[text_block("first observation")]),
        replay_call(content=[text_block("second observation")]),
    ])
    with tracing_context(enabled=False):
        tools = await ToolProvider(await load_mcp_client_provider(config)).discover_tools()
        responses = [await tools[0].ainvoke({
            "name": tools[0].name, "args": {}, "id": str(index), "type": "tool_call",
        }) for index in range(2)]
    assert [response.text for response in responses] == ["first observation", "second observation"]
    assert [record["index"] for record in read_records(tmp_path) if record["phase"] == "started"] == [0, 1]


@pytest.mark.asyncio
async def test_discovery_closes_sessions_and_tools_use_independent_calls(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [
        replay_call(content=[text_block("first"), image_block()]),
        replay_call(content=[text_block("second"), image_block()]),
    ], servers=("first", "second"))
    tools = await ToolProvider(await load_mcp_client_provider(config)).discover_tools()
    assert [tool.name for tool in tools] == ["first_inspect", "second_inspect"]
    snapshot = snapshot_tools(tools)
    assert [tool.source for tool in snapshot.tools] == ["first", "second"]
    assert snapshot.tools[0].annotations.readOnlyHint is True
    assert "env" not in snapshot.model_dump_json()
    for server in ("first", "second"):
        records = read_records(tmp_path, server)
        assert sum(record["phase"] == "closed" for record in records) == 1
        assert sum(record["phase"] == "discovery" for record in records) == 1
        assert not any(record["phase"] == "started" for record in records)
    with tracing_context(enabled=False):
        for index, expected in enumerate(("first", "second")):
            result = await tools[0].ainvoke({
                "name": tools[0].name, "args": {}, "id": str(index), "type": "tool_call",
            })
            assert isinstance(result, ToolMessage)
            assert result.text == expected
            assert result.content_blocks[1]["type"] == "image"
    records = read_records(tmp_path, "first")
    assert len({record["pid"] for record in records}) == 3
    assert sum(record["phase"] == "completed" for record in records) == 2
    assert snapshot_tools(tools) == snapshot


@pytest.mark.asyncio
async def test_configuration_errors_do_not_require_a_device(tmp_path: Path) -> None:
    config = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        await MCPClientProvider(config).load_clients()
    config.write_text('{"mcpServers":{"unsupported":{"command":"unused","transport":"http"}}}', encoding="utf-8")
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="transport"):
        await MCPClientProvider(config).load_clients()


@pytest.mark.asyncio
async def test_additional_tools_are_frozen_per_discovery_and_snapshotted(tmp_path: Path) -> None:
    async def finish_task() -> str:
        """Finish the current task."""
        return "passed"

    extra_tool = StructuredTool.from_function(
        coroutine=finish_task, metadata={"tool_source": "builtin:fixture"},
    )
    additional_tools = [extra_tool]
    provider = ToolProvider(MCPClientProvider(tmp_path / "unused.json"), additional_tools=additional_tools)
    additional_tools.clear()
    first = await provider.discover_tools()
    second = await provider.discover_tools()
    assert first == second == (extra_tool,)
    snapshot = snapshot_tools(first)
    assert snapshot.tools[0].name == extra_tool.name
    assert snapshot.tools[0].source == "builtin:fixture"
    assert snapshot.tools[0].annotations.readOnlyHint is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_mcp", [False, True])
async def test_complete_tool_collection_rejects_duplicate_names(tmp_path: Path, with_mcp: bool) -> None:
    async def inspect() -> str:
        """Inspect the current target."""
        return "observed"

    extra_tool = StructuredTool.from_function(coroutine=inspect, name="fixture_inspect")
    clients = (await load_mcp_client_provider(write_mcp_config(tmp_path, []))
               if with_mcp else MCPClientProvider(tmp_path / "unused.json"))
    provider = ToolProvider(clients, additional_tools=(extra_tool,) if with_mcp else (extra_tool, extra_tool))
    with pytest.raises(ValueError, match="fixture_inspect"):
        await provider.discover_tools()
