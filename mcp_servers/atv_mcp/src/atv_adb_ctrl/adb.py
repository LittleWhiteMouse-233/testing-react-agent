"""Bounded asynchronous ADB commands and device connection checks."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from ipaddress import IPv4Address, IPv6Address

import anyio


class ADBClient:
    """Execute device commands after checking the configured network target."""

    def __init__(self, *, adb_path: str, ip_address: IPv4Address | IPv6Address,
                 operation_timeout_seconds: float) -> None:
        self.adb_path = adb_path
        self.ip_address = ip_address
        self.operation_timeout_seconds = operation_timeout_seconds

    @property
    def serial(self) -> str:
        host = f"[{self.ip_address}]" if isinstance(self.ip_address, IPv6Address) else str(self.ip_address)
        return f"{host}:5555"

    async def run(self, *arguments: str) -> bytes:
        await self.probe()
        return await self._run_adb("-s", self.serial, *arguments)

    async def _run_adb(self, *arguments: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self.adb_path, *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.operation_timeout_seconds)
            if process.returncode:
                diagnostic = (stderr or stdout).decode("utf-8", errors="replace").strip()
                raise RuntimeError(diagnostic or "ADB command failed")
            return stdout
        finally:
            with anyio.CancelScope(shield=True):
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

    async def probe(self) -> None:
        connection_output = ""
        for attempt in range(2):
            try:
                state = await self._run_adb("-s", self.serial, "get-state")
                if state.strip() != b"device":
                    raise RuntimeError(f"ADB target is not ready: {state.decode('utf-8', errors='replace').strip()}")
                return
            except (OSError, RuntimeError, TimeoutError) as exc:
                if attempt:
                    reason = str(exc) or type(exc).__name__
                    raise RuntimeError(f"设备断连: {self.serial}; connect: {connection_output}; {reason}") from exc
            # ADB connect can report failure on stdout with exit code zero.
            # Only the subsequent get-state confirms that the device is usable.
            try:
                connection_output = (await self._run_adb("connect", self.serial)).decode("utf-8", errors="replace").strip()
            except (OSError, RuntimeError, TimeoutError) as exc:
                reason = str(exc) or type(exc).__name__
                raise RuntimeError(f"设备断连: {self.serial}; connect failed: {reason}") from exc
