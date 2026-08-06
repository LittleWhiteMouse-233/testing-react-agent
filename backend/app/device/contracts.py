from __future__ import annotations

from typing import Protocol

from app.domain.tools import (
    DeviceCapabilities,
    DeviceDescription,
    DeviceHealth,
    ScreenshotData,
)


class DeviceProvider(Protocol):
    device_id: str

    async def health(self) -> DeviceHealth: ...
    def capabilities(self) -> DeviceCapabilities: ...
    async def describe(self) -> DeviceDescription: ...
    async def screenshot(self) -> ScreenshotData: ...
