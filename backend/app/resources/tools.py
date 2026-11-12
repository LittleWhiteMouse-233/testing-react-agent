"""Combine supplied tools with MCP discovery through the official LangChain adapter."""
from __future__ import annotations

from collections.abc import Sequence

from fastmcp.client.group import ClientGroup
from langchain.mcp import as_langchain_tool
from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema
from mcp_types import Tool as MCPTool

from app.domain.resources import ToolDefinitionSnapshot, ToolCatalogSnapshot, ToolAnnotationsSnapshot
from app.resources.mcp import MCPClientProvider


class ToolProvider:
    """Discover source definitions, then optionally bind this batch's executable tools."""

    def __init__(self, mcp_clients: MCPClientProvider, *, additional_tools: Sequence[BaseTool] = ()) -> None:
        self.mcp_clients = mcp_clients
        self.additional_tools = tuple(additional_tools)
        self.client_groups: dict[str, ClientGroup] = {
            server_name: ClientGroup({server_name: client}) for server_name, client in mcp_clients.clients.items()
        }
        self._remote_tools_by_server: dict[str, list[MCPTool]] = {}

    async def discover_tools(self) -> ToolCatalogSnapshot:
        definitions: list[ToolDefinitionSnapshot] = []
        self._remote_tools_by_server = {}
        for server_name, group in self.client_groups.items():
            # One-member groups retain prefixes without opening unrelated services.
            client = self.mcp_clients.clients[server_name]
            async with group:
                if client.server_capabilities is None or client.server_capabilities.tools is None:
                    continue
                remote_tools = await group.list_tools(cache_mode="bypass")
                self._remote_tools_by_server[server_name] = remote_tools
                definitions.extend(ToolDefinitionSnapshot(
                    name=remote_tool.name, description=remote_tool.description or "",
                    input_schema=remote_tool.input_schema, source_id=f"mcp:{server_name}",
                    annotations=ToolAnnotationsSnapshot.model_validate(
                        remote_tool.annotations.model_dump() if remote_tool.annotations is not None else {},
                    ),
                ) for remote_tool in remote_tools)
        return ToolCatalogSnapshot(tools=[*definitions, *snapshot_tools(self.additional_tools).tools])

    async def build_execution_tools(self) -> tuple[BaseTool, ...]:
        """Adapt definitions while the caller holds their groups and discovered routes."""
        loaded_tools: list[BaseTool] = []
        for server_name, remote_tools in self._remote_tools_by_server.items():
            group = self.client_groups[server_name]
            for remote_tool in remote_tools:
                adapted_tool = await as_langchain_tool(remote_tool, group)
                adapted_tool.metadata = {**(adapted_tool.metadata or {}), "tool_source": f"mcp:{server_name}"}
                loaded_tools.append(adapted_tool)
        loaded_tools.extend(self.additional_tools)
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
            source_id=str(metadata["tool_source"]),
            annotations=ToolAnnotationsSnapshot.model_validate(annotations),
        ))
    return ToolCatalogSnapshot(tools=definitions)
