"""Discover isolated capability batches and publish complete preview catalogues."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import AsyncExitStack
from pathlib import Path
from collections.abc import Sequence

from langchain_core.tools import BaseTool

from app.config import SettingsService
from app.domain.errors import describe_exception
from app.domain.resources import (
    CapabilityCatalog, CapabilityCatalogChanged, CapabilityColumnSelection,
    build_capability_preview, merge_capability_preview, select_capabilities,
)
from app.resources.mcp import MCPClientProvider, MCPConnectionError
from app.resources.static_resources import SkillResourceProvider
from app.resources.tools import ToolProvider


@dataclass(frozen=True)
class CapabilityDiscovery:
    """Framework boundary: bound executable tools and their credential-free directory facts."""

    tools: tuple[BaseTool, ...]
    catalog: CapabilityCatalog[CapabilityColumnSelection]


class CapabilityProvider:
    """Own serialized discovery; each successful batch retains its own bound clients."""

    def __init__(self, mcp_config_path: Path, skills_dir: Path, builtin_tools: Sequence[BaseTool], *, settings_service: SettingsService) -> None:
        self.mcp_config_path = mcp_config_path
        self.skills_dir = skills_dir
        self.builtin_tools = tuple(builtin_tools)
        self.settings_service = settings_service
        self._discovery_lock = asyncio.Lock()
        self._latest: CapabilityCatalog[CapabilityColumnSelection] | None = None
        self._discovery_error = "Capabilities have not been discovered"

    @property
    def preview(self) -> CapabilityCatalog[CapabilityColumnSelection]:
        """Read the last complete discovery without connecting to external services."""
        if self._latest is None:
            raise MCPConnectionError(self._discovery_error)
        return self._latest

    async def discover(
        self, *, selected_columns: Sequence[CapabilityColumnSelection] | None = None,
        build_execution_tools: bool = False,
    ) -> CapabilityDiscovery:
        """Refresh directory facts, validate selections, and optionally bind executable tools."""
        phase = "MCP configuration loading"
        timeout_seconds = self.settings_service.current.capability_discovery_timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._discovery_lock:
                    cached_preview = self.preview if selected_columns is not None else None
                    selected_source_ids = {column.id for column in selected_columns or () if column.id.startswith("mcp:")}
                    clients = MCPClientProvider(self.mcp_config_path)
                    await clients.load_clients()
                    if selected_columns is not None:
                        clients.clients = {name: client for name, client in clients.clients.items()
                                           if f"mcp:{name}" in selected_source_ids}
                    resources = SkillResourceProvider(self.skills_dir, clients)
                    tool_provider = ToolProvider(clients, additional_tools=(*resources.build_resource_tools(), *self.builtin_tools))
                    selection_error: CapabilityCatalogChanged | None = None
                    async with AsyncExitStack() as connections:
                        for server_name, group in tool_provider.client_groups.items():
                            phase = f"MCP connection: {server_name}"
                            await connections.enter_async_context(group)
                        phase = "MCP tool discovery"
                        tool_catalog = await tool_provider.discover_tools()
                        phase = "Skill and resource discovery"
                        resource_catalog = await resources.discover_resources()
                        phase = "Capability catalog construction"
                        catalog = build_capability_preview(tool_catalog, resource_catalog,
                                                           [f"mcp:{name}" for name in clients.clients])
                        if cached_preview is not None:
                            catalog = merge_capability_preview(cached_preview, catalog, selected_source_ids)
                        if selected_columns is not None:
                            try:
                                select_capabilities(catalog, selected_columns)
                            except CapabilityCatalogChanged as exc:
                                selection_error = exc
                        tools = tool_provider.additional_tools
                        if build_execution_tools and selection_error is None:
                            phase = "MCP tool assembly"
                            tools = await tool_provider.build_execution_tools()
                        phase = "MCP connection cleanup"
                    self._latest = catalog
                    if selection_error is not None:
                        raise selection_error
                    return CapabilityDiscovery(tools=tools, catalog=catalog)
        except (CapabilityCatalogChanged, MCPConnectionError):
            raise
        except Exception as exc:
            self._discovery_error = describe_exception(exc, phase=phase)
            raise MCPConnectionError(self._discovery_error) from exc
