from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langchain_core.tools import BaseTool

from app.domain.activity import AgentActivity
from app.domain.resources.tools import ToolCatalogSnapshot


@dataclass(frozen=True)
class ToolBinding:
    """框架工具与显式 activity 权限及副作用审计分类的运行时绑定。"""

    tool: BaseTool
    scopes: frozenset[AgentActivity]
    changes_device_state: bool


@dataclass(frozen=True)
class DeviceToolManifest:
    """设备或 shared source 在目录合并前声明的工具绑定序列。"""

    bindings: tuple[ToolBinding, ...]


class ToolProvider(Protocol):
    def tools_for(
        self, device_id: str, activity: AgentActivity
    ) -> tuple[BaseTool, ...]: ...

    def snapshot_for(self, device_id: str) -> ToolCatalogSnapshot: ...
