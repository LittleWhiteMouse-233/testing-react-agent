from __future__ import annotations

from dataclasses import dataclass

import pytest
from langchain_core.tools import tool

from app.domain.activity import Activity
from app.domain.tools import DeviceCapabilities, ToolEntry
from app.tools import CatalogToolProvider


@dataclass
class CapabilitySource:
    device_id: str
    entries: tuple[ToolEntry, ...]
    calls: int = 0

    def capabilities(self) -> DeviceCapabilities:
        self.calls += 1
        return DeviceCapabilities(
            device_id=self.device_id,
            provider="test",
            metadata={"fixture": True},
            tools=self.entries,
        )


def test_catalog_preserves_tools_and_matches_activity_values() -> None:
    @tool
    async def act_only(value: str) -> str:
        """Mutate a setting with the supplied value."""
        return value

    @tool
    async def read_only(value: str) -> str:
        """Read a setting with the supplied value."""
        return value

    @tool
    async def future_tool() -> str:
        """Serve a future workflow."""
        return "future"

    source = CapabilitySource(
        "device-a",
        (
            ToolEntry(act_only, frozenset({"act"})),
            ToolEntry(read_only, frozenset({"act", "judge"})),
            ToolEntry(future_tool, frozenset({"future"})),
        ),
    )
    catalog = CatalogToolProvider([source])

    assert source.calls == 1
    assert catalog.tools_for("device-a", Activity.ACT).tools[0] is act_only
    assert catalog.tools_for("device-a", Activity.ACT).names == {
        "act_only",
        "read_only",
    }
    assert catalog.tools_for("device-a", Activity.JUDGE).names == {"read_only"}
    assert catalog.tools_for("device-a", Activity.PLANNING).names == set()
    assert act_only.description == "Mutate a setting with the supplied value."
    assert "value" in act_only.get_input_jsonschema()["required"]

    snapshot = catalog.capabilities_for("device-a")
    assert snapshot.device_id == "device-a"
    assert {item.name for item in snapshot.tools} == {
        "act_only",
        "read_only",
        "future_tool",
    }
    assert next(item for item in snapshot.tools if item.name == "act_only").description == (
        act_only.description
    )


def test_catalog_isolates_device_owners_and_merges_shared_tools() -> None:
    @tool("same_name")
    async def first_device_tool() -> str:
        """Run on the first device."""
        return "first"

    @tool("same_name")
    async def second_device_tool() -> str:
        """Run on the second device."""
        return "second"

    @tool
    async def shared_read() -> str:
        """Read shared state."""
        return "shared"

    catalog = CatalogToolProvider(
        [
            CapabilitySource(
                "first",
                (ToolEntry(first_device_tool, frozenset({"act"})),),
            ),
            CapabilitySource(
                "second",
                (ToolEntry(second_device_tool, frozenset({"act"})),),
            ),
        ],
        shared_capability_sources=(
            CapabilitySource(
                "shared",
                (ToolEntry(shared_read, frozenset({"act", "judge"})),),
            ),
        ),
    )

    first = catalog.tools_for("first", Activity.ACT)
    second = catalog.tools_for("second", Activity.ACT)
    assert first.names == second.names == {"same_name", "shared_read"}
    assert first.tools[0] is first_device_tool
    assert second.tools[0] is second_device_tool
    assert catalog.tools_for("first", Activity.JUDGE).names == {
        "shared_read"
    }
    assert {tool.name for tool in catalog.capabilities_for("first").tools} == {
        "same_name",
        "shared_read",
    }


def test_catalog_rejects_invalid_scopes_duplicates_and_reserved_names() -> None:
    @tool("duplicate")
    def first() -> str:
        """First tool."""
        return "first"

    @tool("duplicate")
    def duplicate() -> str:
        """Duplicate tool."""
        return "duplicate"

    with pytest.raises(ValueError, match="at least one scope"):
        CatalogToolProvider(
            [CapabilitySource("empty", (ToolEntry(first, frozenset()),))]
        )
    with pytest.raises(ValueError, match="invalid scope"):
        CatalogToolProvider(
            [CapabilitySource("blank", (ToolEntry(first, frozenset({" "})),))]
        )
    with pytest.raises(ValueError, match="Duplicate"):
        CatalogToolProvider(
            [
                CapabilitySource(
                    "duplicate",
                    (
                        ToolEntry(first, frozenset({"act"})),
                        ToolEntry(duplicate, frozenset({"act"})),
                    ),
                )
            ]
        )

    @tool("finish_task")
    def reserved() -> str:
        """Reserved tool."""
        return "reserved"

    with pytest.raises(ValueError, match="reserved"):
        CatalogToolProvider(
            [CapabilitySource("reserved", (ToolEntry(reserved, frozenset({"act"})),))]
        )

    with pytest.raises(TypeError, match="frozenset"):
        CatalogToolProvider(
            [CapabilitySource("mutable", (ToolEntry(first, {"act"}),))]  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="device collision view"):
        CatalogToolProvider(
            [
                CapabilitySource(
                    "collision",
                    (ToolEntry(first, frozenset({"act"})),),
                )
            ],
            shared_capability_sources=(
                CapabilitySource(
                    "shared",
                    (ToolEntry(duplicate, frozenset({"judge"})),),
                ),
            ),
        )
