from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langchain_core.tools import BaseTool

from app.domain.activity import Activity
from app.domain.tools import DeviceCapabilitiesSnapshot


@dataclass(frozen=True)
class ToolSet:
    tools: tuple[BaseTool, ...]
    names: frozenset[str]


class ToolProvider(Protocol):
    def tools_for(self, device_id: str, activity: Activity) -> ToolSet: ...

    def names_for(
        self,
        device_id: str,
        *activities: Activity,
    ) -> frozenset[str]: ...

    def capabilities_for(self, device_id: str) -> DeviceCapabilitiesSnapshot: ...
