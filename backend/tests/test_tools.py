from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from app.domain.activity import AgentActivity
from app.domain.resources.tools import ToolCatalogSnapshot
from app.tools import CatalogToolProvider, DeviceToolManifest, ToolBinding


@dataclass
class ManifestSource:
    device_id: str
    manifest: DeviceToolManifest

    def tool_manifest(self) -> DeviceToolManifest:
        return self.manifest


def test_catalog_uses_declared_scopes_and_derives_audit_snapshot() -> None:
    @tool
    async def mutate(value: str) -> str:
        """Mutate device state."""
        return value

    @tool
    async def observe(value: str) -> str:
        """Observe device state."""
        return value

    source = ManifestSource(
        "device-a",
        DeviceToolManifest(
            bindings=(
                ToolBinding(
                    mutate,
                    frozenset({AgentActivity.ACT, AgentActivity.JUDGE}),
                    True,
                ),
                ToolBinding(
                    observe,
                    frozenset({AgentActivity.ACT, AgentActivity.JUDGE}),
                    False,
                ),
            ),
        ),
    )
    catalog = CatalogToolProvider([source])

    assert [item.name for item in catalog.tools_for("device-a", AgentActivity.ACT)] == [
        "mutate",
        "observe",
    ]
    assert [item.name for item in catalog.tools_for("device-a", AgentActivity.JUDGE)] == [
        "mutate",
        "observe",
    ]
    snapshot = catalog.snapshot_for("device-a")
    assert {item.name for item in snapshot.tools} == {"mutate", "observe"}
    mutate_capability = next(item for item in snapshot.tools if item.name == "mutate")
    assert mutate_capability.changes_device_state
    assert mutate_capability.scopes == [AgentActivity.ACT, AgentActivity.JUDGE]
    assert not hasattr(snapshot, "device_id")
    with pytest.raises(ValidationError, match="names must be unique"):
        ToolCatalogSnapshot(tools=[snapshot.tools[0], snapshot.tools[0]])


def test_catalog_rejects_reserved_and_duplicate_tool_names() -> None:
    @tool("finish_task")
    def reserved() -> str:
        """Reserved tool."""
        return "reserved"

    with pytest.raises(ValueError, match="reserved"):
        CatalogToolProvider(
            [
                ManifestSource(
                    "device-a",
                    DeviceToolManifest(
                        bindings=(
                            ToolBinding(
                                reserved, frozenset({AgentActivity.ACT}), False
                            ),
                        ),
                    ),
                )
            ]
        )


def test_catalog_keeps_device_tools_isolated_and_injects_shared_tools_in_order() -> None:
    @tool("native")
    def native_a() -> str:
        """Native tool for device A."""
        return "a"

    @tool("native")
    def native_b() -> str:
        """Native tool for device B."""
        return "b"

    @tool("shared_observe")
    def shared_observe() -> str:
        """Shared observation helper."""
        return "shared"

    device_a_source = ManifestSource(
        "device-a",
        DeviceToolManifest(
            bindings=(
                ToolBinding(native_a, frozenset({AgentActivity.ACT}), True),
            )
        ),
    )
    device_b_source = ManifestSource(
        "device-b",
        DeviceToolManifest(
            bindings=(
                ToolBinding(native_b, frozenset({AgentActivity.ACT}), True),
            )
        ),
    )
    shared_source = ManifestSource(
        "shared-source",
        DeviceToolManifest(
            bindings=(
                ToolBinding(
                    shared_observe,
                    frozenset({AgentActivity.ACT, AgentActivity.JUDGE}),
                    False,
                ),
            )
        ),
    )

    catalog = CatalogToolProvider(
        [device_a_source, device_b_source],
        shared_tool_sources=[shared_source],
    )

    device_a_tools = catalog.tools_for("device-a", AgentActivity.ACT)
    device_b_tools = catalog.tools_for("device-b", AgentActivity.ACT)
    assert [tool.name for tool in device_a_tools] == ["native", "shared_observe"]
    assert [tool.name for tool in device_b_tools] == ["native", "shared_observe"]
    assert device_a_tools[0] is native_a
    assert device_b_tools[0] is native_b
    assert catalog.tools_for("device-a", AgentActivity.JUDGE) == (shared_observe,)
    assert catalog.tools_for("device-b", AgentActivity.JUDGE) == (shared_observe,)


def test_catalog_rejects_device_and_shared_tool_name_collisions() -> None:
    @tool("same")
    def device_tool() -> str:
        """Device-native tool."""
        return "device"

    @tool("same")
    def shared_tool() -> str:
        """Shared tool."""
        return "shared"

    device_source = ManifestSource(
        "device-a",
        DeviceToolManifest(
            bindings=(
                ToolBinding(device_tool, frozenset({AgentActivity.ACT}), True),
            )
        ),
    )
    shared_source = ManifestSource(
        "shared-source",
        DeviceToolManifest(
            bindings=(
                ToolBinding(shared_tool, frozenset({AgentActivity.ACT}), False),
            )
        ),
    )

    with pytest.raises(ValueError, match="device-a.*same"):
        CatalogToolProvider(
            [device_source],
            shared_tool_sources=[shared_source],
        )

    @tool("same")
    def first() -> str:
        """First."""
        return "first"

    @tool("same")
    def second() -> str:
        """Second."""
        return "second"

    with pytest.raises(ValueError, match="Duplicate"):
        CatalogToolProvider(
            [
                ManifestSource(
                    "device-a",
                    DeviceToolManifest(
                        bindings=(
                            ToolBinding(first, frozenset({AgentActivity.ACT}), False),
                            ToolBinding(second, frozenset({AgentActivity.ACT}), False),
                        ),
                    ),
                )
            ]
        )
