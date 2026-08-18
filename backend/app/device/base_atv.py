from __future__ import annotations

import asyncio
import re
from enum import StrEnum

from langchain_core.tools import tool
from pydantic import Field, create_model

from app.domain.errors import (
    ActionTimeout,
    CaptureFailed,
    DeviceUnavailable,
    UnsupportedAction,
)
from app.device.contracts import ScreenshotCapture
from app.domain.activity import AgentActivity
from app.domain.resources.device import DeviceHealth, DeviceInfo
from app.tools.contracts import DeviceToolManifest, ToolBinding


class RemoteKey(StrEnum):
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"
    DPAD_CENTER = "DPAD_CENTER"
    BACK = "BACK"
    HOME = "HOME"
    MENU = "MENU"
    POWER = "POWER"
    VOLUME_UP = "VOLUME_UP"
    VOLUME_DOWN = "VOLUME_DOWN"
    MUTE = "MUTE"
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    STOP = "STOP"
    NEXT = "NEXT"
    PREVIOUS = "PREVIOUS"
    TAB = "TAB"
    ENTER = "ENTER"
    DEL = "DEL"
    DIGIT_0 = "DIGIT_0"
    DIGIT_1 = "DIGIT_1"
    DIGIT_2 = "DIGIT_2"
    DIGIT_3 = "DIGIT_3"
    DIGIT_4 = "DIGIT_4"
    DIGIT_5 = "DIGIT_5"
    DIGIT_6 = "DIGIT_6"
    DIGIT_7 = "DIGIT_7"
    DIGIT_8 = "DIGIT_8"
    DIGIT_9 = "DIGIT_9"

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
    provider = "adb"

    def __init__(
        self,
        device_id: str,
        adb_path: str = "adb",
        timeout_seconds: float = 15,
    ) -> None:
        self.device_id = device_id
        self.adb_path = adb_path
        self.timeout_seconds = timeout_seconds
        self._tool_manifest = self._build_tool_manifest()

    def _build_tool_manifest(self) -> DeviceToolManifest:
        press_args = create_model("AdbPressKeyArgs", key=(RemoteKey, ...))

        @tool(args_schema=press_args)
        async def device_press_key(key: RemoteKey) -> str:
            """Press one supported Android TV remote key."""
            return await self.press(key)

        input_args = create_model(
            "AdbInputTextArgs",
            text=(str, Field(min_length=1, max_length=1000)),
        )

        @tool(args_schema=input_args)
        async def device_input_text(text: str) -> str:
            """Enter text into the currently focused TV input."""
            return await self.input_text(text)

        wait_args = create_model(
            "AdbWaitArgs",
            duration_ms=(int, Field(ge=100, le=10_000)),
        )

        @tool(args_schema=wait_args)
        async def device_wait(duration_ms: int) -> str:
            """Wait briefly without changing the tested business state."""
            return await self.wait(duration_ms)

        return DeviceToolManifest(
            bindings=(
                ToolBinding(device_press_key, frozenset({AgentActivity.ACT}), True),
                ToolBinding(device_input_text, frozenset({AgentActivity.ACT}), True),
                ToolBinding(
                    device_wait,
                    frozenset({AgentActivity.ACT, AgentActivity.JUDGE}),
                    False,
                ),
            ),
        )

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

    def tool_manifest(self) -> DeviceToolManifest:
        return self._tool_manifest

    async def describe(self) -> DeviceInfo:
        model, resolution, locale = await asyncio.gather(
            self._run("shell", "getprop", "ro.product.model"),
            self._run("shell", "wm", "size"),
            self._run("shell", "getprop", "persist.sys.locale"),
        )
        return DeviceInfo(
            model=str(model) or None,
            resolution=str(resolution).removeprefix("Physical size: ") or None,
            locale=str(locale) or None,
        )

    async def screenshot(self) -> ScreenshotCapture:
        try:
            content = await self._run("exec-out", "screencap", "-p", binary=True)
        except (DeviceUnavailable, ActionTimeout) as exc:
            raise CaptureFailed(str(exc)) from exc
        if not isinstance(content, bytes) or not content.startswith(b"\x89PNG"):
            raise CaptureFailed("ADB returned an invalid PNG screenshot")
        return ScreenshotCapture(content=content)

    async def press(self, key: RemoteKey) -> str:
        code = _KEY_CODES.get(key)
        if not code:
            raise UnsupportedAction(str(key))
        await self._run("shell", "input", "keyevent", code)
        return f"Pressed {key.value}"

    async def input_text(self, text: str) -> str:
        # Android input accepts %s for spaces. Reject control characters rather
        # than passing shell-like input through to the device.
        if re.search(r"[\x00-\x1f\x7f]", text):
            raise UnsupportedAction("Text contains control characters")
        encoded = text.replace("%", r"\%").replace(" ", "%s")
        await self._run("shell", "input", "text", encoded)
        return "Entered text"

    async def wait(self, duration_ms: int) -> str:
        await asyncio.sleep(duration_ms / 1000)
        return f"Waited {duration_ms} ms"
