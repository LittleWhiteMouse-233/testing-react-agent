from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from app.domain.activity import AgentActivity
from app.domain.tools import ToolCatalogSnapshot
from app.tools import CatalogToolProvider, DeviceToolManifest, ToolBinding


@dataclass
class ManifestSource:
    device_id: str
    manifest: DeviceToolManifest

    def tool_manifest(self) -> DeviceToolManifest:
        return self.manifest


def test_catalog_filters_state_changes_from_judge_and_derives_snapshot() -> None:
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
        "observe"
    ]
    snapshot = catalog.snapshot_for("device-a")
    assert {item.name for item in snapshot.tools} == {"mutate", "observe"}
    mutate_capability = next(item for item in snapshot.tools if item.name == "mutate")
    assert mutate_capability.changes_device_state
    assert mutate_capability.scopes == [AgentActivity.ACT]
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
