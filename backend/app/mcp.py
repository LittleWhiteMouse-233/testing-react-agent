"""Standard MCP configuration and framework-owned client lifecycle."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import describe_exception


class MCPServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    transport: Literal["stdio"] = "stdio"


class MCPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcpServers: dict[str, MCPServerSettings]


class MCPConnectionError(RuntimeError):
    """Configured services could not be opened or discovered."""


async def load_mcp_clients(config_path: Path) -> dict[str, Client[Any]]:
    """Read configuration once per run; clients connect only inside public contexts."""
    try:
        settings = MCPSettings.model_validate_json(
            await asyncio.to_thread(config_path.read_text, encoding="utf-8")
        )
        clients: dict[str, Client[Any]] = {}
        for server_name, server in settings.mcpServers.items():
            if not server_name.strip():
                raise ValueError("MCP server names must not be blank")
            cwd = Path(server.cwd or ".")
            if not cwd.is_absolute():
                cwd = config_path.parent / cwd
            clients[server_name] = Client(StdioTransport(
                command=server.command, args=server.args, env=server.env,
                cwd=str(cwd.resolve()), keep_alive=False,
            ))
        return clients
    except Exception as exc:
        raise MCPConnectionError(describe_exception(exc, phase="Invalid MCP configuration")) from exc
