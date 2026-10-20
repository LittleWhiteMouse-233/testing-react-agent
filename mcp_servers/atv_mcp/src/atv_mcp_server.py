"""Independent Android TV stdio service, configured solely by its YAML file."""
from __future__ import annotations

import argparse
import asyncio
import base64
import re
from pathlib import Path
from typing import Annotated, Literal

import anyio
from anyio.lowlevel import checkpoint
import yaml
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp_types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from atv_adb_ctrl import ATVController, ATVControlSettings, RemoteKey


class ATVServerSettings(ATVControlSettings):
    """Flat YAML configuration extending device settings with MCP queue policy."""

    queue_max_operations: int = Field(default=16, ge=1)
    queue_timeout_seconds: float = Field(default=60, gt=0)
    wait_min_ms: int = Field(default=100, ge=0)
    wait_max_ms: int = Field(default=10000, ge=0)
    post_action_wait_ms: int = Field(default=1000, ge=0)

    @model_validator(mode="after")
    def wait_limits_are_ordered(self) -> "ATVServerSettings":
        if self.wait_max_ms < self.wait_min_ms:
            raise ValueError("wait_max_ms must be at least wait_min_ms")
        return self


class PressKeyOperation(BaseModel):
    """One remote key; available values are discovered through the capability tool."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["press_key"]
    key: RemoteKey


class ScreenshotOperation(BaseModel):
    """Discriminated queue member representing a screenshot with no parameters."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["screenshot"]


class WaitOperation(BaseModel):
    """Explicit queue delay in milliseconds."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["wait"]
    duration_ms: int


class TextOperation(BaseModel):
    """Text entered into the focused input control."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=1000)


Operation = Annotated[PressKeyOperation | ScreenshotOperation | WaitOperation | TextOperation, Field(discriminator="type")]


def build_server(settings: ATVServerSettings) -> FastMCP:
    server = FastMCP("android-tv")
    controller = ATVController(settings)
    execution_lock = asyncio.Lock()

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_device_capabilities() -> dict:
        """Discover supported remote key values and descriptions, target identity and operation limits."""
        async with execution_lock:
            await controller.probe()
        return {
            "serial": controller.serial,
            "keys": [{"key": key.value, "description": key.value.replace("_", " ").lower()} for key in RemoteKey],
            "limits": settings.model_dump(exclude={"adb_path", "ip_address"}),
        }

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False), output_schema=None)
    async def execute_operations(operations: list[Operation]) -> ToolResult:
        """Execute an ordered queue of press_key, screenshot, wait and text operations.

        Discover valid keys and limits with get_device_capabilities. Consecutive
        screenshots are forbidden. Key/text actions settle automatically unless
        followed by an explicit wait. Text/image results preserve execution order.
        A failure stops the queue; never blindly repeat an unconfirmed action.
        """
        content: list[TextContent | ImageContent] = []
        position = 0
        execution_started = False
        try:
            if not 1 <= len(operations) <= settings.queue_max_operations:
                raise ValueError(f"Queue length must be 1..{settings.queue_max_operations}")
            for index, operation in enumerate(operations):
                position = index
                if isinstance(operation, WaitOperation):
                    if not settings.wait_min_ms <= operation.duration_ms <= settings.wait_max_ms:
                        raise ValueError("Wait duration is outside configured limits")
                elif isinstance(operation, TextOperation):
                    if re.search(r"[\x00-\x1f\x7f]", operation.text):
                        raise ValueError("Text contains control characters")
                elif (isinstance(operation, ScreenshotOperation) and index
                      and isinstance(operations[index - 1], ScreenshotOperation)):
                    raise ValueError("Consecutive screenshots are forbidden")
            async with execution_lock:
                position = 0
                deadline = anyio.current_time() + settings.queue_timeout_seconds
                with anyio.fail_after(settings.queue_timeout_seconds):
                    await controller.probe()
                    execution_started = True
                    for position, operation in enumerate(operations):
                        await checkpoint()
                        # Honor request cancellation between bounded device commands.
                        with anyio.CancelScope(shield=True), anyio.fail_after(
                            min(settings.operation_timeout_seconds, max(0, deadline - anyio.current_time()))
                        ):
                            if isinstance(operation, ScreenshotOperation):
                                image = await controller.screenshot()
                                encoded = await asyncio.to_thread(lambda: base64.b64encode(image).decode("ascii"))
                                content.append(ImageContent(type="image", data=encoded, mime_type="image/png"))
                            elif isinstance(operation, PressKeyOperation):
                                await controller.press_key(operation.key)
                                content.append(TextContent(type="text", text=f"{position + 1}: Pressed {operation.key}"))
                            elif isinstance(operation, TextOperation):
                                await controller.input_text(operation.text)
                                content.append(TextContent(type="text", text=f"{position + 1}: Entered text"))
                        await checkpoint()
                        if isinstance(operation, WaitOperation):
                            with anyio.fail_after(settings.operation_timeout_seconds):
                                await controller.wait(operation.duration_ms)
                            content.append(TextContent(type="text", text=f"{position + 1}: Waited {operation.duration_ms} ms"))
                        elif isinstance(operation, (PressKeyOperation, TextOperation)):
                            explicit_wait = position + 1 < len(operations) and isinstance(operations[position + 1], WaitOperation)
                            if not explicit_wait and settings.post_action_wait_ms:
                                await controller.wait(settings.post_action_wait_ms)
                                last = content[-1]
                                assert isinstance(last, TextContent)
                                last.text += f"; settled for {settings.post_action_wait_ms} ms"
            return ToolResult(content=list(content))
        except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
            failure = (
                f"Queue stopped at operation {position + 1}; remaining operations were not executed"
                if execution_started else
                f"Queue rejected before execution at operation {position + 1}; no operations were executed"
            )
            reason = str(exc) or type(exc).__name__
            content.append(TextContent(type="text", text=f"{failure}: {reason}"))
            return ToolResult(is_error=True, content=list(content))

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args()
    settings = ATVServerSettings.model_validate(yaml.safe_load(arguments.config.read_text(encoding="utf-8")))
    build_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
