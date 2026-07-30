from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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


class DeviceHealth(BaseModel):
    available: bool
    message: str = ""


class DeviceCapabilities(BaseModel):
    screenshot: bool = True
    input_text: bool = True
    supported_keys: list[RemoteKey] = Field(default_factory=lambda: list(RemoteKey))
    model: str | None = None
    resolution: str | None = None
    locale: str | None = None


class ScreenshotData(BaseModel):
    content: bytes
    mime_type: str = "image/png"
    activity: str | None = None


class ActionResult(BaseModel):
    success: bool = True
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    changes_device_state: bool = False
    timeout_seconds: float = 15
    source: str = "builtin"


class ToolContext(BaseModel):
    run_id: str
    task_run_id: str | None = None
    device_id: str


class ToolResult(BaseModel):
    success: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any]
    decision_summary: str = Field(min_length=1)


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    TIMED_OUT = "timed_out"
    INVALID = "invalid"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ToolExecutionResult(BaseModel):
    status: ToolExecutionStatus
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
