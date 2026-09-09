import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from app.tools import MCPConnectionError, MCPToolProvider, snapshot_tools
from mcp_support import read_records, replay_call, write_mcp_config


@pytest.mark.asyncio
async def test_stdio_discovery_prefixes_and_reuses_sessions(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [replay_call(), replay_call()], servers=("first", "second"))
    async with MCPToolProvider(config).connect() as tools:
        assert [tool.name for tool in tools] == ["first_inspect", "second_inspect"]
        snapshot = snapshot_tools(tools)
        assert [tool.source for tool in snapshot.tools] == ["first", "second"]
        assert snapshot.tools[0].annotations.readOnlyHint is True
        assert "env" not in snapshot.model_dump_json()
        for index in range(2):
            result = await tools[0].ainvoke({"name": tools[0].name, "args": {}, "id": str(index), "type": "tool_call"})
            assert isinstance(result, ToolMessage)
            assert result.content_blocks[0]["type"] == "image"
    records = read_records(tmp_path, "first")
    assert len({entry["pid"] for entry in records}) == 1
    assert len([entry for entry in records if entry["phase"] == "completed"]) == 2


@pytest.mark.asyncio
async def test_configuration_errors_do_not_require_a_device(tmp_path: Path) -> None:
    config = tmp_path / "missing.json"
    with pytest.raises(MCPConnectionError, match="configuration"):
        async with MCPToolProvider(config).connect():
            pass
    config.write_text('{"mcpServers":{"unsupported":{"command":"unused","transport":"http"}}}', encoding="utf-8")
    with pytest.raises(MCPConnectionError, match="configuration"):
        async with MCPToolProvider(config).connect():
            pass


@pytest.mark.asyncio
async def test_connection_failure_is_a_public_mcp_error(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"missing": {"command": "nonexistent-mcp-executable"}}}), encoding="utf-8")
    with pytest.raises(MCPConnectionError, match="connection or discovery"):
        async with MCPToolProvider(config).connect():
            pass


@pytest.mark.asyncio
async def test_discovery_timeout_is_a_public_mcp_error(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [])
    with pytest.raises(MCPConnectionError):
        async with MCPToolProvider(config, timeout_seconds=0.001).connect():
            pass
