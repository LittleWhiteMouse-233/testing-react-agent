"""Reusable Android TV operations without MCP or queue policy."""
from __future__ import annotations

import shlex
from ipaddress import IPv4Address, IPv6Address

import anyio
from anyio.lowlevel import checkpoint
from pydantic import BaseModel, ConfigDict, Field

from .adb import ADBClient
from .keys import RemoteKey


class ATVControlSettings(BaseModel):
    """Device settings shared by scripts and the MCP server configuration."""

    model_config = ConfigDict(extra="forbid")
    adb_path: str = "adb"
    ip_address: IPv4Address | IPv6Address
    operation_timeout_seconds: float = Field(default=15, gt=0)
    capture_max_attempts: int = Field(default=3, ge=1)


class ATVController:
    """Asynchronous device operations; callers own sequencing and settling delays."""

    def __init__(self, settings: ATVControlSettings) -> None:
        self.settings = settings
        self._adb_client = ADBClient(
            adb_path=settings.adb_path,
            ip_address=settings.ip_address,
            operation_timeout_seconds=settings.operation_timeout_seconds,
        )

    @property
    def serial(self) -> str:
        return self._adb_client.serial

    async def probe(self) -> None:
        await self._adb_client.probe()

    async def run(self, *arguments: str) -> bytes:
        """Check the device and run one ADB command, without replaying failures."""
        return await self._adb_client.run(*arguments)

    async def press_key(self, key: RemoteKey) -> None:
        await self.run("shell", "input", "keyevent", key.code)

    async def input_text(self, text: str) -> None:
        # ADB shell joins arguments on-device; quote for that boundary.
        encoded_text = text.replace(" ", "%s")
        await self.run("shell", "input", "text", shlex.quote(encoded_text))

    async def wait(self, duration_ms: int) -> None:
        """Wait cooperatively; MCP wait limits and deadlines belong to the caller."""
        await anyio.sleep(duration_ms / 1000)

    async def screenshot(self) -> bytes:
        failure_reason = ""
        for _ in range(self.settings.capture_max_attempts):
            await checkpoint()
            try:
                image = await self.run("exec-out", "screencap", "-p")
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("Invalid PNG screenshot")
                return image
            except (RuntimeError, TimeoutError, OSError) as exc:
                failure_reason = str(exc) or type(exc).__name__
        raise RuntimeError(f"Screenshot failed: {failure_reason}")
