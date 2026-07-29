from __future__ import annotations

from app.domain.models import (
    ActionResult,
    DeviceCapabilities,
    DeviceHealth,
    RemoteKey,
    ScreenshotData,
)


from typing import Protocol


class DeviceController(Protocol):
    device_id: str

    async def health(self) -> DeviceHealth: ...
    async def capabilities(self) -> DeviceCapabilities: ...
    async def screenshot(self) -> ScreenshotData: ...
    async def press(self, key: RemoteKey) -> ActionResult: ...
    async def input_text(self, text: str) -> ActionResult: ...
    async def wait(self, duration_ms: int) -> ActionResult: ...
