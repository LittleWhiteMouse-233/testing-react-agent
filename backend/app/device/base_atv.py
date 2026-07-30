from __future__ import annotations

from app.domain.errors import (
    ActionTimeout,
    CaptureFailed,
    DeviceUnavailable,
    UnsupportedAction,
)
from app.domain.tools import (
    ActionResult,
    DeviceCapabilities,
    DeviceHealth,
    RemoteKey,
    ScreenshotData,
)
import asyncio
import re

_KEY_CODES: dict[RemoteKey, str] = {
    RemoteKey.DPAD_UP: "19",
    RemoteKey.DPAD_DOWN: "20",
    RemoteKey.DPAD_LEFT: "21",
    RemoteKey.DPAD_RIGHT: "22",
    RemoteKey.DPAD_CENTER: "23",
    RemoteKey.BACK: "4",
    RemoteKey.HOME: "3",
    RemoteKey.MENU: "82",
    RemoteKey.POWER: "26",
    RemoteKey.VOLUME_UP: "24",
    RemoteKey.VOLUME_DOWN: "25",
    RemoteKey.MUTE: "164",
    RemoteKey.PLAY: "126",
    RemoteKey.PAUSE: "127",
    RemoteKey.STOP: "86",
    RemoteKey.NEXT: "87",
    RemoteKey.PREVIOUS: "88",
    RemoteKey.TAB: "61",
    RemoteKey.ENTER: "66",
    RemoteKey.DEL: "67",
    **{RemoteKey[f"DIGIT_{i}"]: str(7 + i) for i in range(10)},
}


class AdbDeviceController:
    def __init__(
        self,
        device_id: str,
        adb_path: str = "adb",
        timeout_seconds: float = 15,
    ) -> None:
        self.device_id = device_id
        self.adb_path = adb_path
        self.timeout_seconds = timeout_seconds

    async def _run(self, *args: str, binary: bool = False) -> bytes | str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.adb_path,
                "-s",
                self.device_id,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            raise ActionTimeout(f"ADB command timed out: {' '.join(args)}") from exc
        except OSError as exc:
            raise DeviceUnavailable(str(exc)) from exc
        if process.returncode:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise DeviceUnavailable(message or "ADB command failed")
        return stdout if binary else stdout.decode("utf-8", errors="replace").strip()

    async def health(self) -> DeviceHealth:
        try:
            state = await self._run("get-state")
            return DeviceHealth(available=state == "device", message=str(state))
        except (DeviceUnavailable, ActionTimeout) as exc:
            return DeviceHealth(available=False, message=str(exc))

    async def capabilities(self) -> DeviceCapabilities:
        model, resolution, locale = await asyncio.gather(
            self._run("shell", "getprop", "ro.product.model"),
            self._run("shell", "wm", "size"),
            self._run("shell", "getprop", "persist.sys.locale"),
        )
        return DeviceCapabilities(
            model=str(model) or None,
            resolution=str(resolution).removeprefix("Physical size: ") or None,
            locale=str(locale) or None,
        )

    async def screenshot(self) -> ScreenshotData:
        try:
            content = await self._run("exec-out", "screencap", "-p", binary=True)
        except (DeviceUnavailable, ActionTimeout) as exc:
            raise CaptureFailed(str(exc)) from exc
        if not isinstance(content, bytes) or not content.startswith(b"\x89PNG"):
            raise CaptureFailed("ADB returned an invalid PNG screenshot")
        return ScreenshotData(content=content)

    async def press(self, key: RemoteKey) -> ActionResult:
        code = _KEY_CODES.get(key)
        if not code:
            raise UnsupportedAction(str(key))
        await self._run("shell", "input", "keyevent", code)
        return ActionResult(summary=f"Pressed {key.value}")

    async def input_text(self, text: str) -> ActionResult:
        # Android input accepts %s for spaces. Reject control characters rather
        # than passing shell-like input through to the device.
        if re.search(r"[\x00-\x1f\x7f]", text):
            raise UnsupportedAction("Text contains control characters")
        encoded = text.replace("%", r"\%").replace(" ", "%s")
        await self._run("shell", "input", "text", encoded)
        return ActionResult(summary="Entered text")

    async def wait(self, duration_ms: int) -> ActionResult:
        await asyncio.sleep(duration_ms / 1000)
        return ActionResult(summary=f"Waited {duration_ms} ms")
