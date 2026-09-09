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
from langchain_mcp_adapters.sessions import Connection
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel, ConfigDict, Field

from app.domain.resources.tools import ToolCapability, ToolCatalogSnapshot, ToolAnnotationsSnapshot


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


class MCPToolProvider:
    """Opens and closes all service sessions inside the owning run task."""

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
            raise MCPConnectionError(f"Invalid MCP configuration: {exc}") from exc
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
                raise body_error from exc
            raise MCPConnectionError(f"MCP connection or discovery failed: {exc}") from exc
        if body_error is not None:
            raise body_error

    async def _discover(
        self, client: MultiServerMCPClient, connections: dict[str, Connection],
        sessions: AsyncExitStack,
    ) -> list[BaseTool]:
        loaded_tools: list[BaseTool] = []
        for server_name in connections:
            session = await sessions.enter_async_context(
                client.session(server_name, auto_initialize=False)
            )
            await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
            tools = await asyncio.wait_for(
                load_mcp_tools(session, server_name=server_name, tool_name_prefix=True),
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

    capabilities: list[ToolCapability] = []
    for tool in tools:
        schema = tool.tool_call_schema
        metadata = tool.metadata or {}
        capabilities.append(ToolCapability(
            name=tool.name, description=tool.description or "",
            input_schema=schema if isinstance(schema, dict) else model_json_schema(schema),
            source=str(metadata["mcp_server_name"]),
            annotations=ToolAnnotationsSnapshot.model_validate(metadata),
        ))
    return ToolCatalogSnapshot(tools=capabilities)
