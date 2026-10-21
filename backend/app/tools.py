"""Import MCP business tools through the official LangChain adapter."""
from __future__ import annotations

from collections.abc import Sequence

from fastmcp.client.group import ClientGroup
from langchain.mcp import as_langchain_tool
from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema

from app.domain.resources.tools import ToolDefinitionSnapshot, ToolCatalogSnapshot, ToolAnnotationsSnapshot
from app.mcp import MCPClientProvider


class MCPToolProvider:
    """Discover once per run; returned tools own their invocation contexts."""

    def __init__(self, mcp_clients: MCPClientProvider) -> None:
        self.mcp_clients = mcp_clients

    async def discover_tools(self) -> tuple[BaseTool, ...]:
        loaded_tools: list[BaseTool] = []
        for server_name, client in self.mcp_clients.clients.items():
            # One-member groups retain prefixes without opening unrelated services.
            group = ClientGroup({server_name: client})
            async with group:
                if client.server_capabilities is None or client.server_capabilities.tools is None:
                    continue
                for remote_tool in await group.list_tools(cache_mode="bypass"):
                    adapted_tool = await as_langchain_tool(remote_tool, group)
                    adapted_tool.metadata = {**(adapted_tool.metadata or {}), "mcp_server_name": server_name}
                    loaded_tools.append(adapted_tool)
        names = [entry.name for entry in loaded_tools]
        if len(names) != len(set(names)) or set(names) & {"finish_task", "load_skill", "read_resource", "list_resources"}:
            raise ValueError("MCP tool names collide")
        return tuple(loaded_tools)


def snapshot_tools(tools: Sequence[BaseTool]) -> ToolCatalogSnapshot:
    """Freeze credential-free business tool definitions and standard annotations."""
    definitions: list[ToolDefinitionSnapshot] = []
    for entry in tools:
        schema = entry.tool_call_schema
        metadata = entry.metadata or {}
        annotations = metadata.get("mcp", {}).get("tool", {}).get("annotations", {})
        definitions.append(ToolDefinitionSnapshot(
            name=entry.name, description=entry.description or "",
            input_schema=schema if isinstance(schema, dict) else model_json_schema(schema),
            source=str(metadata["mcp_server_name"]),
            annotations=ToolAnnotationsSnapshot.model_validate(annotations),
        ))
    return ToolCatalogSnapshot(tools=definitions)
