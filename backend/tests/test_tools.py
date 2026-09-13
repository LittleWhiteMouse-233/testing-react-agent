import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.errors import GraphBubbleUp
from mcp import ClientSession

from app.domain.errors import ToolCleanupError
from app.tools import MCPConnectionError, MCPToolProvider, _MCPServerSession, snapshot_tools
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


@pytest.mark.asyncio
async def test_cancelled_call_closes_session_before_later_call(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [replay_call(delay=10)])
    async with MCPToolProvider(config).connect() as tools:
        invocation = asyncio.create_task(tools[0].ainvoke({}))
        for _ in range(200):
            if any(entry['phase'] == 'started' for entry in read_records(tmp_path)):
                break
            await asyncio.sleep(0.01)
        assert any(entry['phase'] == 'started' for entry in read_records(tmp_path))
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        records = read_records(tmp_path)
        assert any(entry['phase'] == 'closed' for entry in records)
        assert not any(entry['phase'] == 'completed' for entry in records)
        assert len({entry['pid'] for entry in records}) == 1
        write_mcp_config(tmp_path, [replay_call()])
        started = time.perf_counter()
        result = await tools[0].ainvoke({'name': tools[0].name, 'args': {}, 'id': 'next', 'type': 'tool_call'})
        assert isinstance(result, ToolMessage)
        assert result.content_blocks[0]['type'] == 'image'
        (tmp_path / 'recovery-seconds.txt').write_text(str(time.perf_counter() - started))
    records = read_records(tmp_path)
    assert len({entry['pid'] for entry in records}) == 2
    assert sum(entry['phase'] == 'started' for entry in records) == 2
    assert sum(entry['phase'] == 'completed' for entry in records) == 1
    assert sum(entry['phase'] == 'closed' for entry in records) == 2
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith('mcp-session-')]


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_fails', [False, True])
@pytest.mark.parametrize('already_chained', [False, True])
@pytest.mark.parametrize('body_error_kind', ['application', 'graph_control', 'graph_control_group'])
async def test_body_exception_preserved_after_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     cleanup_fails: bool, already_chained: bool,
                                                     body_error_kind: str) -> None:
    config = write_mcp_config(tmp_path, [])
    provider = MCPToolProvider(config)
    closed = asyncio.Event()
    cleanup_error = RuntimeError('cleanup failed')
    body_error = (
        GraphBubbleUp('graph control') if body_error_kind == 'graph_control' else
        ExceptionGroup('mixed controls', [GraphBubbleUp('graph control'), OSError('execution failed')])
        if body_error_kind == 'graph_control_group' else ValueError('application failed')
    )
    prior_cause = OSError('original execution failed') if already_chained else None
    body_error.__cause__ = prior_cause

    @asynccontextmanager
    async def connection():
        try:
            yield
        finally:
            closed.set()
            if cleanup_fails:
                raise cleanup_error

    async def discover(client, connections, sessions, on_cleanup_failure=None):
        await sessions.enter_async_context(connection())
        return []

    monkeypatch.setattr(provider, '_discover', discover)
    with pytest.raises(type(body_error)) as captured:
        async with provider.connect():
            raise body_error
    assert closed.is_set()
    assert captured.value is body_error
    if cleanup_fails and already_chained:
        assert isinstance(captured.value.__cause__, ExceptionGroup)
        assert captured.value.__cause__.exceptions == (prior_cause, cleanup_error)
    else:
        assert captured.value.__cause__ is (cleanup_error if cleanup_fails else prior_cause)


@pytest.mark.asyncio
async def test_body_cancellation_preserved_after_stdio_cleanup(tmp_path: Path) -> None:
    config = write_mcp_config(tmp_path, [])
    opened = asyncio.Event()

    async def caller():
        async with MCPToolProvider(config).connect():
            opened.set()
            await asyncio.Event().wait()

    invocation = asyncio.create_task(caller())
    await asyncio.wait_for(opened.wait(), 10)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation
    assert any(entry['phase'] == 'closed' for entry in read_records(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_session_cleanup_timeout_is_sticky_and_not_restarted(late_failure: bool) -> None:
    session = create_autospec(ClientSession, instance=True)
    client = create_autospec(MultiServerMCPClient, instance=True)
    release = asyncio.Event()
    close_count = 0
    failures: list[BaseException] = []

    @asynccontextmanager
    async def connection(*args, **kwargs):
        nonlocal close_count
        try:
            yield session
        finally:
            close_count += 1
            await release.wait()
            if late_failure:
                raise OSError("late close failure")

    client.session.side_effect = connection
    owner = _MCPServerSession(client, "fixture", 1, 0.03, failures.append)
    await owner.session()
    with pytest.raises(ToolCleanupError) as first:
        await asyncio.wait_for(owner.close(), 1)
    assert close_count == 1 and failures
    with pytest.raises(ToolCleanupError) as repeated:
        await owner.close()
    assert repeated.value is first.value
    release.set()
    assert owner._owner is not None
    await asyncio.gather(owner._owner, return_exceptions=True)
    with pytest.raises(ToolCleanupError) as unavailable:
        await owner.session()
    assert unavailable.value is first.value
    assert close_count == 1
    if late_failure:
        assert any(isinstance(failure, OSError) for failure in failures)


@pytest.mark.asyncio
async def test_initialization_failure_and_cleanup_failure_are_both_preserved() -> None:
    session = create_autospec(ClientSession, instance=True)
    session.initialize.side_effect = OSError("initialize failed")
    client = create_autospec(MultiServerMCPClient, instance=True)
    failures: list[BaseException] = []

    @asynccontextmanager
    async def connection(*args, **kwargs):
        try:
            yield session
        finally:
            raise OSError("close failed")

    client.session.side_effect = connection
    owner = _MCPServerSession(client, "fixture", 1, 0.1, failures.append)
    with pytest.raises(MCPConnectionError) as initialization:
        await owner.session()
    assert str(initialization.value.__cause__) == "initialize failed"
    with pytest.raises(ToolCleanupError) as cleanup:
        await owner.close()
    assert str(cleanup.value.__cause__) == "close failed"
    assert failures
