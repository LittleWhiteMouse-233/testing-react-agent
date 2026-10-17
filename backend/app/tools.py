"""Standard stdio MCP configuration and framework-managed tool discovery."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection
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


class MCPToolProvider:
    """Discover tools once; the adapter owns each invocation's session."""

    def __init__(self, config_path: Path, timeout_seconds: float = 120) -> None:
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds

    async def discover_tools(self) -> tuple[BaseTool, ...]:
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
            client = MultiServerMCPClient(connections, tool_name_prefix=True)
        except Exception as exc:
            raise MCPConnectionError(describe_exception(exc, phase="Invalid MCP configuration")) from exc

        loaded_tools: list[BaseTool] = []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                for server_name in connections:
                    server_tools = await client.get_tools(server_name=server_name)
                    for tool in server_tools:
                        tool.metadata = {**(tool.metadata or {}), "mcp_server_name": server_name}
                    loaded_tools.extend(server_tools)
            names = [tool.name for tool in loaded_tools]
            if len(names) != len(set(names)) or "finish_task" in names:
                raise ValueError("MCP tool names collide")
        except Exception as exc:
            raise MCPConnectionError(describe_exception(exc, phase="MCP connection or discovery failed")) from exc
        return tuple(loaded_tools)


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
