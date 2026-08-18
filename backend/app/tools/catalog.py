from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema

from app.domain.activity import AgentActivity
from app.domain.resources.tools import ToolCapability, ToolCatalogSnapshot
from app.tools.contracts import DeviceToolManifest, ToolBinding


class _ToolManifestSource(Protocol):
    """只声明一份设备工具 manifest 的运行时来源。"""

    device_id: str

    def tool_manifest(self) -> DeviceToolManifest: ...


def _validate_binding(binding: ToolBinding) -> None:
    if not isinstance(binding.tool, BaseTool):
        raise TypeError("ToolBinding.tool must be a LangChain BaseTool")
    if not isinstance(binding.scopes, frozenset) or not binding.scopes:
        raise ValueError(f"Tool {binding.tool.name} must declare scopes")
    if any(not isinstance(scope, AgentActivity) for scope in binding.scopes):
        raise TypeError("ToolBinding scopes must contain AgentActivity values")
    if not binding.tool.name.strip():
        raise ValueError("Tool names must not be blank")
    if binding.tool.name == "finish_task":
        raise ValueError("Tool uses reserved name: finish_task")
    schema = binding.tool.tool_call_schema
    if not isinstance(schema, dict):
        model_json_schema(schema)


def _validate_manifest(manifest: DeviceToolManifest, *, owner: str) -> None:
    names: set[str] = set()
    for binding in manifest.bindings:
        _validate_binding(binding)
        if binding.tool.name in names:
            raise ValueError(f"Duplicate tool name {binding.tool.name!r} for {owner}")
        names.add(binding.tool.name)


class CatalogToolProvider:
    """The one merged runtime catalog for device-native and shared tools."""

    def __init__(
        self,
        device_tool_sources: Sequence[_ToolManifestSource],
        *,
        shared_tool_sources: Sequence[_ToolManifestSource] = (),
    ) -> None:
        manifests_by_device: dict[str, DeviceToolManifest] = {}
        for source in device_tool_sources:
            manifest = source.tool_manifest()
            _validate_manifest(manifest, owner=f"device {source.device_id}")
            if source.device_id in manifests_by_device:
                raise ValueError(f"Duplicate device manifest: {source.device_id}")
            manifests_by_device[source.device_id] = manifest
        if not manifests_by_device:
            raise ValueError("At least one device tool manifest is required")

        shared_bindings: list[ToolBinding] = []
        for source in shared_tool_sources:
            manifest = source.tool_manifest()
            _validate_manifest(manifest, owner="shared")
            shared_bindings.extend(manifest.bindings)

        self._bindings: dict[str, tuple[ToolBinding, ...]] = {}
        for device_id, manifest in manifests_by_device.items():
            bindings = (*manifest.bindings, *shared_bindings)
            names = [binding.tool.name for binding in bindings]
            collisions = {name for name in names if names.count(name) > 1}
            if collisions:
                raise ValueError(
                    f"Duplicate tool names in device {device_id} view: {sorted(collisions)}"
                )
            self._bindings[device_id] = tuple(bindings)

    def _bindings_for(self, device_id: str) -> tuple[ToolBinding, ...]:
        try:
            return self._bindings[device_id]
        except KeyError as exc:
            raise LookupError(f"Device tools are not registered: {device_id}") from exc

    def tools_for(
        self, device_id: str, activity: AgentActivity
    ) -> tuple[BaseTool, ...]:
        return tuple(
            binding.tool
            for binding in self._bindings_for(device_id)
            if activity in binding.scopes
        )

    def snapshot_for(self, device_id: str) -> ToolCatalogSnapshot:
        return ToolCatalogSnapshot(
            tools=[self._to_capability(binding) for binding in self._bindings_for(device_id)]
        )

    @staticmethod
    def _to_capability(binding: ToolBinding) -> ToolCapability:
        schema = binding.tool.tool_call_schema
        input_schema = schema if isinstance(schema, dict) else model_json_schema(schema)
        return ToolCapability(
            name=binding.tool.name,
            description=binding.tool.description or "",
            input_schema=input_schema,
            scopes=sorted(
                binding.scopes,
                key=lambda scope: scope.value,
            ),
            changes_device_state=binding.changes_device_state,
        )
