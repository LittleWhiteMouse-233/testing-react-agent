from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.resources.device import DeviceHealth, DeviceInfo
from app.tools.contracts import DeviceToolManifest


@dataclass(frozen=True)
class ScreenshotCapture:
    """Transient integration payload. Raw bytes never enter domain contracts."""

    content: bytes
    mime_type: str = "image/png"
    activity: str | None = None


class DeviceProvider(Protocol):
    device_id: str
    provider: str

    async def health(self) -> DeviceHealth: ...
    def tool_manifest(self) -> DeviceToolManifest: ...
    async def describe(self) -> DeviceInfo: ...
    async def screenshot(self) -> ScreenshotCapture: ...
