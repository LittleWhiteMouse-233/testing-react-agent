from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.device.test_fake import FakeDeviceController, ReplayDeviceController
from app.domain.errors import CaptureFailed, UnsupportedAction
from app.device.base_atv import RemoteKey


@pytest.mark.asyncio
async def test_fake_device_supports_capture_failure_injection() -> None:
    device = FakeDeviceController(capture_failures=2)
    with pytest.raises(CaptureFailed):
        await device.screenshot()
    with pytest.raises(CaptureFailed):
        await device.screenshot()
    assert (await device.screenshot()).content.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_replay_rejects_unexpected_action() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        fixture_dir = Path(directory)
        frame = (await FakeDeviceController().screenshot()).content
        (fixture_dir / "001.png").write_bytes(frame)
        device = ReplayDeviceController(
            fixture_dir,
            expected_actions=[{"type": "PRESS_KEY", "key": "DPAD_CENTER"}],
        )
        with pytest.raises(UnsupportedAction):
            await device.press(RemoteKey.BACK)
