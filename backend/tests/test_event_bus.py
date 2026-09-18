from __future__ import annotations

import pytest

from app.event_stream import EventBus


@pytest.mark.asyncio
async def test_event_bus_fans_out_to_every_subscriber() -> None:
    bus = EventBus[int]()
    async with bus.subscribe("run-1") as first, bus.subscribe("run-1") as second:
        await bus.publish("run-1", 7)
        assert await first.get() == 7
        assert await second.get() == 7


@pytest.mark.asyncio
async def test_streams_are_isolated_and_overflow_disconnects_only_slow_subscriber() -> None:
    bus = EventBus[int]()
    other_bus = EventBus[int]()
    async with bus.subscribe("case-1") as slow, bus.subscribe("case-2") as other_case, other_bus.subscribe("case-1") as other_family:
        async with bus.subscribe("case-1") as fast:
            for sequence in range(slow.maxsize + 1):
                await bus.publish("case-1", sequence)
                assert await fast.get() == sequence
            assert await slow.get() is None
            await bus.publish("case-1", 2000)
            assert await fast.get() == 2000
            assert slow.empty() and other_case.empty() and other_family.empty()
        await bus.publish("case-1", 2001)
        assert fast.empty()
