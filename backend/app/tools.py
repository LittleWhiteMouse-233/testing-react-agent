"""Standard stdio MCP configuration and run-scoped LangChain tool discovery."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langchain_mcp_adapters.sessions import Connection
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from app.domain.resources.tools import ToolDefinitionSnapshot, ToolCatalogSnapshot, ToolAnnotationsSnapshot
from app.domain.errors import describe_exception


class MCPServerSettings(BaseModel):
    """Standard stdio connection; paths are relative to the JSON configuration."""

    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    transport: Literal["stdio"] = "stdio"


class MCPSettings(BaseModel):
    """Framework multi-server JSON configuration boundary."""

    model_config = ConfigDict(extra="forbid")
    mcpServers: dict[str, MCPServerSettings]


class MCPConnectionError(RuntimeError):
    """Configured MCP services could not be opened or discovered."""


class _MCPServerSession:
    """Own SDK context entry/exit in one task; discard cancelled sessions lazily.

    Adapter tools capture a session permanently. Its public interceptor instead
    routes requests to this run-owned session, while the SDK owns protocol and
    process cleanup and the adapter still owns schemas and result conversion.
    """

    def __init__(self, client: MultiServerMCPClient, server_name: str, timeout_seconds: float) -> None:
        self.client = client
        self.server_name = server_name
        self.timeout_seconds = timeout_seconds
        self._owner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[ClientSession] | None = None
        self._close_requested = asyncio.Event()

    async def session(self) -> ClientSession:
        if self._owner is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._close_requested = asyncio.Event()
            self._owner = asyncio.create_task(self._own_session(), name=f"mcp-session-{self.server_name}")
        assert self._ready is not None
        return await asyncio.shield(self._ready)

    async def _own_session(self) -> None:
        assert self._ready is not None
        try:
            async with self.client.session(self.server_name, auto_initialize=False) as session:
                await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                self._ready.set_result(session)
                await self._close_requested.wait()
        except Exception as exc:
            if not self._ready.done():
                failure = MCPConnectionError("MCP initialization failed")
                failure.__cause__ = exc
                self._ready.set_exception(failure)
            else:
                raise
        finally:
            if not self._ready.done():
                self._ready.cancel()

    async def close(self) -> None:
        if self._owner is None:
            return
        self._close_requested.set()
        if self._ready is not None and not self._ready.done():
            self._owner.cancel()
        try:
            await self._owner
        except asyncio.CancelledError:
            if not self._owner.cancelled():
                raise
        finally:
            if self._ready is not None and self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
            self._owner = None
            self._ready = None

    async def call_tool(
        self, request: MCPToolCallRequest,
        handler: object,
    ) -> CallToolResult:
        initialized = False
        try:
            session = await self.session()
            initialized = True
            return await session.call_tool(request.name, request.args)
        except asyncio.CancelledError:
            await self.close()
            if not initialized:
                raise MCPConnectionError("MCP reinitialization interrupted before tool execution") from None
            raise


class MCPToolProvider:
    """Run-scoped connections, with each SDK context kept in one owning task."""

    def __init__(self, config_path: Path, timeout_seconds: float = 120) -> None:
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[tuple[BaseTool, ...]]:
        try:
            settings = MCPSettings.model_validate_json(
                await asyncio.to_thread(self.config_path.read_text, encoding="utf-8")
            )
            connections: dict[str, Connection] = {}
            for server_name, server in settings.mcpServers.items():
                if not server_name.strip():
                    raise ValueError("MCP server names must not be blank")
                cwd = Path(server.cwd or ".")
                if not cwd.is_absolute():
                    cwd = self.config_path.parent / cwd
                connections[server_name] = {
                    "transport": "stdio", "command": server.command,
                    "args": server.args, "env": server.env,
                    "cwd": str(cwd.resolve()),
                }
            client = MultiServerMCPClient(connections)
        except Exception as exc:
            raise MCPConnectionError(describe_exception(exc, phase="Invalid MCP configuration")) from exc
        body_error: BaseException | None = None
        try:
            async with AsyncExitStack() as sessions:
                loaded_tools = await self._discover(client, connections, sessions)
                try:
                    yield tuple(loaded_tools)
                except BaseException as exc:
                    # Application errors must not pass through SDK task groups.
                    body_error = exc
        except Exception as exc:
            if body_error is not None:
                prior_cause = body_error.__cause__ or (
                    body_error.__context__ if not body_error.__suppress_context__ else None
                )
                if prior_cause is not None:
                    raise body_error from BaseExceptionGroup(
                        "Execution cause and MCP cleanup failures", [prior_cause, exc],
                    )
                raise body_error from exc
            raise MCPConnectionError(describe_exception(exc, phase="MCP connection or discovery failed")) from exc
        if body_error is not None:
            raise body_error

    async def _discover(
        self, client: MultiServerMCPClient, connections: dict[str, Connection],
        sessions: AsyncExitStack,
    ) -> list[BaseTool]:
        loaded_tools: list[BaseTool] = []
        for server_name in connections:
            server_session = _MCPServerSession(client, server_name, self.timeout_seconds)
            sessions.push_async_callback(server_session.close)
            session = await server_session.session()
            tools = await asyncio.wait_for(
                load_mcp_tools(session, server_name=server_name, tool_name_prefix=True,
                               tool_interceptors=[server_session.call_tool]),
                timeout=self.timeout_seconds,
            )
            for tool in tools:
                tool.metadata = {**(tool.metadata or {}), "mcp_server_name": server_name}
            loaded_tools.extend(tools)
        names = [tool.name for tool in loaded_tools]
        if len(names) != len(set(names)) or "finish_task" in names:
            raise ValueError("MCP tool names collide")
        return loaded_tools


def snapshot_tools(tools: Sequence[BaseTool]) -> ToolCatalogSnapshot:
    """Project discovered definitions to the immutable, credential-free run catalog."""

    definitions: list[ToolDefinitionSnapshot] = []
    for tool in tools:
        schema = tool.tool_call_schema
        metadata = tool.metadata or {}
        definitions.append(ToolDefinitionSnapshot(
            name=tool.name, description=tool.description or "",
            input_schema=schema if isinstance(schema, dict) else model_json_schema(schema),
            source=str(metadata["mcp_server_name"]),
            annotations=ToolAnnotationsSnapshot.model_validate(metadata),
        ))
    return ToolCatalogSnapshot(tools=definitions)
