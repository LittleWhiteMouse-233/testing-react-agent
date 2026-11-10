"""Discover isolated capability batches and publish complete preview catalogues."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from langchain_core.tools import BaseTool

from app.domain.errors import describe_exception
from app.domain.resources import CapabilityCatalog, CapabilityColumnSelection, build_capability_preview
from app.resources.mcp import MCPClientProvider, MCPConnectionError
from app.resources.static_resources import SkillResourceProvider
from app.resources.tools import ToolProvider, snapshot_tools


@dataclass(frozen=True)
class CapabilityDiscovery:
    """Framework boundary: bound executable tools and their credential-free directory facts."""

    tools: tuple[BaseTool, ...]
    catalog: CapabilityCatalog[CapabilityColumnSelection]


class CapabilityProvider:
    """Own serialized discovery; each successful batch retains its own bound clients."""

    def __init__(self, mcp_config_path: Path, skills_dir: Path, builtin_tools: Sequence[BaseTool]) -> None:
        self.mcp_config_path = mcp_config_path
        self.skills_dir = skills_dir
        self.builtin_tools = tuple(builtin_tools)
        self._discovery_lock = asyncio.Lock()
        self._latest: CapabilityDiscovery | None = None
        self._discovery_error = "Capabilities have not been discovered"

    @property
    def preview(self) -> CapabilityCatalog[CapabilityColumnSelection]:
        """Read the last complete discovery without connecting to external services."""
        if self._latest is None:
            raise MCPConnectionError(self._discovery_error)
        return self._latest.catalog

    async def discover(self, timeout_seconds: float) -> CapabilityDiscovery:
        """Prepare and atomically publish a fresh batch within one shared timeout."""
        phase = "MCP configuration loading"
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._discovery_lock:
                    clients = MCPClientProvider(self.mcp_config_path)
                    await clients.load_clients()
                    resources = SkillResourceProvider(self.skills_dir, clients)
                    tool_provider = ToolProvider(clients, additional_tools=(*resources.build_resource_tools(), *self.builtin_tools))
                    phase = "MCP tool discovery"
                    tools = await tool_provider.discover_tools()
                    phase = "Skill and resource discovery"
                    resource_catalog = await resources.discover_resources()
                    catalog = build_capability_preview(snapshot_tools(tools), resource_catalog,
                                                       [f"mcp:{name}" for name in clients.clients])
                    discovery = CapabilityDiscovery(tools=tools, catalog=catalog)
                    self._latest = discovery
                    return discovery
        except Exception as exc:
            self._discovery_error = describe_exception(exc, phase=phase)
            raise MCPConnectionError(self._discovery_error) from exc
