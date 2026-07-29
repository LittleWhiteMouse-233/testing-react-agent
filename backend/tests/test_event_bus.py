from __future__ import annotations

import pytest

from app.services.event_bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_fans_out_to_every_subscriber() -> None:
    bus = EventBus()
    async with bus.subscribe("run-1") as first, bus.subscribe("run-1") as second:
        await bus.publish("run-1", 7)
        assert await first.get() == 7
        assert await second.get() == 7

