"""Combine supplied tools with MCP discovery through the official LangChain adapter."""
from __future__ import annotations

from collections.abc import Sequence
from collections import Counter

from fastmcp.client.group import ClientGroup
from langchain.mcp import as_langchain_tool
from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema

from app.domain.resources.tools import ToolDefinitionSnapshot, ToolCatalogSnapshot, ToolAnnotationsSnapshot
from app.mcp import MCPClientProvider


class ToolProvider:
    """Discover once per run; returned tools own their invocation contexts."""

    def __init__(self, mcp_clients: MCPClientProvider, *, additional_tools: Sequence[BaseTool] = ()) -> None:
        self.mcp_clients = mcp_clients
        self.additional_tools = tuple(additional_tools)

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
                    adapted_tool.metadata = {**(adapted_tool.metadata or {}), "tool_source": server_name}
                    loaded_tools.append(adapted_tool)
        loaded_tools.extend(self.additional_tools)
        name_counts = Counter(entry.name for entry in loaded_tools)
        collisions = sorted(name for name, count in name_counts.items() if count > 1)
        if collisions:
            raise ValueError(f"Tool names collide: {', '.join(collisions)}")
        return tuple(loaded_tools)


def snapshot_tools(tools: Sequence[BaseTool]) -> ToolCatalogSnapshot:
    """Freeze every available tool's model-visible definition and standard annotations."""
    definitions: list[ToolDefinitionSnapshot] = []
    for entry in tools:
        schema = entry.tool_call_schema
        metadata = entry.metadata or {}
        annotations = metadata.get("mcp", {}).get("tool", {}).get("annotations", {})
        definitions.append(ToolDefinitionSnapshot(
            name=entry.name, description=entry.description or "",
            input_schema=schema if isinstance(schema, dict) else model_json_schema(schema),
            source=str(metadata["tool_source"]),
            annotations=ToolAnnotationsSnapshot.model_validate(annotations),
        ))
    return ToolCatalogSnapshot(tools=definitions)
