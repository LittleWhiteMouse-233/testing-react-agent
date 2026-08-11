from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langchain_core.tools import BaseTool

from app.domain.activity import AgentActivity
from app.domain.tools import ToolCatalogSnapshot


@dataclass(frozen=True)
class ToolBinding:
    """Runtime-only binding between a framework tool and product policy."""

    tool: BaseTool
    scopes: frozenset[AgentActivity]
    changes_device_state: bool


@dataclass(frozen=True)
class DeviceToolManifest:
    """A device provider's native tools before shared tools are merged."""

    bindings: tuple[ToolBinding, ...]


class ToolProvider(Protocol):
    def tools_for(
        self, device_id: str, activity: AgentActivity
    ) -> tuple[BaseTool, ...]: ...

    def snapshot_for(self, device_id: str) -> ToolCatalogSnapshot: ...
