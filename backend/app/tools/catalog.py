from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema

from app.domain.activity import Activity
from app.domain.tools import (
    DeviceCapabilities,
    DeviceCapabilitiesSnapshot,
    ToolEntry,
)
from app.tools.contracts import ToolSet


class _CapabilitySource(Protocol):
    device_id: str

    def capabilities(self) -> DeviceCapabilities: ...


def _validate_entry(entry: ToolEntry) -> None:
    if not isinstance(entry.tool, BaseTool):
        raise TypeError("ToolEntry.tool must be a LangChain BaseTool")
    if not isinstance(entry.scopes, frozenset):
        raise TypeError("ToolEntry.scopes must be a frozenset of strings")
    if not entry.scopes:
        raise ValueError(f"Tool {entry.tool.name} must declare at least one scope")
    for scope in entry.scopes:
        if not isinstance(scope, str) or not scope.strip() or scope != scope.strip():
            raise ValueError(
                f"Tool {entry.tool.name} has an invalid scope: {scope!r}"
            )
    if not entry.tool.name.strip():
        raise ValueError("Tool names must not be blank")
    if entry.tool.name == "finish_task":
        raise ValueError("Tool uses reserved name: finish_task")
    schema = entry.tool.tool_call_schema
    if not isinstance(schema, dict):
        model_json_schema(schema)


def _index_entries(
    entries: Iterable[ToolEntry],
    *,
    owner: str,
) -> dict[str, tuple[BaseTool, ...]]:
    indexed: dict[str, list[BaseTool]] = defaultdict(list)
    names: set[str] = set()
    for entry in entries:
        _validate_entry(entry)
        if entry.tool.name in names:
            raise ValueError(
                f"Duplicate tool name {entry.tool.name!r} for {owner}"
            )
        names.add(entry.tool.name)
        for scope in entry.scopes:
            indexed[scope].append(entry.tool)
    return {scope: tuple(tools) for scope, tools in indexed.items()}


@dataclass(frozen=True)
class _DeviceToolView:
    device_id: str
    tools_by_scope: Mapping[str, ToolSet]

    def tools_for(self, activity: Activity) -> ToolSet:
        return self.tools_by_scope.get(
            activity.value,
            ToolSet(tools=(), names=frozenset()),
        )

    def names_for(self, *activities: Activity) -> frozenset[str]:
        names: set[str] = set()
        for activity in activities:
            names.update(self.tools_for(activity).names)
        return frozenset(names)


class CatalogToolProvider:
    """Immutable catalog of pre-decorated tools grouped by owner and scope."""

    def __init__(
        self,
        capability_sources: Sequence[_CapabilitySource],
        *,
        shared_capability_sources: Sequence[_CapabilitySource] = (),
    ) -> None:
        capabilities_by_device: dict[str, DeviceCapabilities] = {}
        for source in capability_sources:
            capabilities = source.capabilities()
            if capabilities.device_id != source.device_id:
                raise ValueError(
                    "Device capability id does not match its provider: "
                    f"{capabilities.device_id} != {source.device_id}"
                )
            if capabilities.device_id in capabilities_by_device:
                raise ValueError(
                    f"Duplicate device capabilities: {capabilities.device_id}"
                )
            if not capabilities.provider.strip():
                raise ValueError("Device capability provider must not be blank")
            capabilities_by_device[capabilities.device_id] = capabilities
        if not capabilities_by_device:
            raise ValueError("At least one device capability declaration is required")

        shared_entries: list[ToolEntry] = []
        for source in shared_capability_sources:
            shared_entries.extend(source.capabilities().tools)
        shared = tuple(shared_entries)
        shared_index = _index_entries(shared, owner="shared")

        self._snapshots: dict[str, DeviceCapabilitiesSnapshot] = {}
        self._views: dict[str, _DeviceToolView] = {}
        for device_id, capabilities in capabilities_by_device.items():
            device_index = _index_entries(
                capabilities.tools,
                owner=f"device {device_id}",
            )
            device_names = {entry.tool.name for entry in capabilities.tools}
            shared_names = {entry.tool.name for entry in shared}
            collisions = device_names & shared_names
            if collisions:
                raise ValueError(
                    f"Duplicate tool names in device {device_id} view: "
                    f"{sorted(collisions)}"
                )
            scopes = device_index.keys() | shared_index.keys()
            tools_by_scope: dict[str, ToolSet] = {}
            for scope in scopes:
                tools = (*device_index.get(scope, ()), *shared_index.get(scope, ()))
                names = [tool.name for tool in tools]
                if len(names) != len(set(names)):
                    raise ValueError(
                        f"Duplicate tool names in device {device_id} scope {scope!r}"
                    )
                tools_by_scope[scope] = ToolSet(
                    tools=tools,
                    names=frozenset(names),
                )
            self._views[device_id] = _DeviceToolView(
                device_id=device_id,
                tools_by_scope=MappingProxyType(tools_by_scope),
            )
            self._snapshots[device_id] = DeviceCapabilities(
                device_id=capabilities.device_id,
                provider=capabilities.provider,
                metadata=dict(capabilities.metadata),
                tools=(*capabilities.tools, *shared),
            ).to_snapshot()

    def tools_for(self, device_id: str, activity: Activity) -> ToolSet:
        return self._view_for(device_id).tools_for(activity)

    def names_for(
        self,
        device_id: str,
        *activities: Activity,
    ) -> frozenset[str]:
        return self._view_for(device_id).names_for(*activities)

    def _view_for(self, device_id: str) -> _DeviceToolView:
        try:
            return self._views[device_id]
        except KeyError as exc:
            raise LookupError(f"Device tools are not registered: {device_id}") from exc

    def capabilities_for(self, device_id: str) -> DeviceCapabilitiesSnapshot:
        try:
            return self._snapshots[device_id].model_copy(deep=True)
        except KeyError as exc:
            raise LookupError(f"Device tools are not registered: {device_id}") from exc
