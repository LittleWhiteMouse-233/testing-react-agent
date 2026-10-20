import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from langsmith import tracing_context

from app.mcp import MCPConnectionError
from app.tools import MCPToolProvider, snapshot_tools
from mcp_support import image_block, read_records, replay_call, text_block, write_mcp_config


@pytest.mark.asyncio
async def test_replay_scenario_continues_across_framework_sessions(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [
        replay_call(content=[text_block("first observation")]),
        replay_call(content=[text_block("second observation")]),
    ])
    with tracing_context(enabled=False):
        tools = await MCPToolProvider(config).discover_tools()
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
    tools = await MCPToolProvider(config).discover_tools()
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
    with pytest.raises(MCPConnectionError, match="configuration"):
        await MCPToolProvider(config).discover_tools()
    config.write_text('{"mcpServers":{"unsupported":{"command":"unused","transport":"http"}}}', encoding="utf-8")
    with pytest.raises(MCPConnectionError, match="configuration"):
        await MCPToolProvider(config).discover_tools()


@pytest.mark.asyncio
async def test_connection_failure_is_a_public_mcp_error(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"missing": {"command": "nonexistent-mcp-executable"}}}), encoding="utf-8")
    with pytest.raises(MCPConnectionError, match="discovery"):
        await MCPToolProvider(config).discover_tools()


@pytest.mark.asyncio
async def test_discovery_timeout_is_a_public_mcp_error(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [])
    with pytest.raises(MCPConnectionError, match="discovery"):
        await MCPToolProvider(config, timeout_seconds=0.001).discover_tools()
