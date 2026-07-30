from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import base64

from app.domain.errors import CaptureFailed, UnsupportedAction
from app.domain.tools import (
    ActionResult,
    DeviceCapabilities,
    DeviceHealth,
    RemoteKey,
    ScreenshotData,
)

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeDeviceController:
    def __init__(
        self,
        device_id: str = "fake-tv",
        frames: Iterable[bytes] | None = None,
        available: bool = True,
        capture_failures: int = 0,
    ) -> None:
        self.device_id = device_id
        self.available = available
        self.frames = list(frames or [_PNG_1X1])
        self.capture_failures = capture_failures
        self.frame_index = 0
        self.actions: list[dict[str, object]] = []

    async def health(self) -> DeviceHealth:
        return DeviceHealth(available=self.available, message="fake device")

    async def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            model="Fake Android TV", resolution="1920x1080", locale="zh-CN"
        )

    async def screenshot(self) -> ScreenshotData:
        if not self.available:
            raise CaptureFailed("Fake device is unavailable")
        if self.capture_failures > 0:
            self.capture_failures -= 1
            raise CaptureFailed("Injected screenshot failure")
        frame = self.frames[min(self.frame_index, len(self.frames) - 1)]
        return ScreenshotData(content=frame, activity=f"fake/frame/{self.frame_index}")

    async def press(self, key: RemoteKey) -> ActionResult:
        self.actions.append({"type": "PRESS_KEY", "key": key.value})
        self.frame_index = min(self.frame_index + 1, len(self.frames) - 1)
        return ActionResult(summary=f"Pressed {key.value}")

    async def input_text(self, text: str) -> ActionResult:
        self.actions.append({"type": "INPUT_TEXT", "text": text})
        self.frame_index = min(self.frame_index + 1, len(self.frames) - 1)
        return ActionResult(summary="Entered text")

    async def wait(self, duration_ms: int) -> ActionResult:
        self.actions.append({"type": "WAIT", "duration_ms": duration_ms})
        return ActionResult(summary=f"Waited {duration_ms} ms")


class ReplayDeviceController(FakeDeviceController):
    def __init__(
        self,
        fixture_dir: Path,
        expected_actions: list[dict[str, object]],
        device_id: str = "replay-tv",
    ) -> None:
        frames = [path.read_bytes() for path in sorted(fixture_dir.glob("*.png"))]
        if not frames:
            raise ValueError("Replay fixture directory must contain PNG files")
        super().__init__(device_id=device_id, frames=frames)
        self.expected_actions = expected_actions

    def _verify(self, action: dict[str, object]) -> None:
        index = len(self.actions)
        if (
            index >= len(self.expected_actions)
            or self.expected_actions[index] != action
        ):
            raise UnsupportedAction(
                f"Replay action mismatch at index {index}: {action}"
            )

    async def press(self, key: RemoteKey) -> ActionResult:
        self._verify({"type": "PRESS_KEY", "key": key.value})
        return await super().press(key)

    async def input_text(self, text: str) -> ActionResult:
        self._verify({"type": "INPUT_TEXT", "text": text})
        return await super().input_text(text)

    async def wait(self, duration_ms: int) -> ActionResult:
        self._verify({"type": "WAIT", "duration_ms": duration_ms})
        return await super().wait(duration_ms)
