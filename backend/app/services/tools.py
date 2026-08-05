from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from langgraph.types import Command
from pydantic import Field, create_model

from app.device.contracts import DeviceController
from app.domain.errors import ActionTimeout, TransientToolError
from app.domain.planning import TaskType
from app.domain.tools import (
    DeviceCapabilities,
    RemoteKey,
    ToolExecutionResult,
    ToolExecutionStatus,
)

ToolExecutor = Callable[
    [ToolCallRequest], Awaitable[ToolMessage | Command]
]
AsyncToolCallWrapper = Callable[
    [ToolCallRequest, ToolExecutor], Awaitable[ToolMessage | Command]
]


@dataclass(frozen=True)
class ToolBuildRequest:
    activity: TaskType
    device: DeviceController
    capabilities: DeviceCapabilities
    enabled_external_tool_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolSet:
    tools: list[BaseTool]
    names: frozenset[str]
    awrap_tool_call: AsyncToolCallWrapper


class ToolProvider(Protocol):
    async def build_tools(self, request: ToolBuildRequest) -> ToolSet: ...


class FrameworkToolProvider:
    """Adapts standard BaseTool instances for the task graph."""

    def __init__(self, *, action_timeout_seconds: float = 15) -> None:
        self.action_timeout_seconds = action_timeout_seconds
        self._external_tools: dict[str, BaseTool] = {}

    _BUILTIN_NAMES = {
        "device_press_key",
        "device_input_text",
        "device_wait",
    }

    def register(self, value: BaseTool) -> None:
        if not isinstance(value, BaseTool):
            raise TypeError("External tools must be LangChain BaseTool instances")
        if value.name == "finish_task" or value.name in self._BUILTIN_NAMES:
            raise ValueError(f"External tool uses reserved name: {value.name}")
        if value.name in self._external_tools:
            raise ValueError(f"Duplicate external tool name: {value.name}")
        self._validated_scopes(value)
        value.metadata = {
            "atv.source": "external",
            **(value.metadata or {}),
        }
        self._external_tools[value.name] = value

    async def build_tools(self, request: ToolBuildRequest) -> ToolSet:
        tools = self._device_tools(request)
        configured = request.enabled_external_tool_names
        configured_external = configured - self._BUILTIN_NAMES
        unknown = configured_external - self._external_tools.keys()
        if unknown:
            raise ValueError(
                f"Enabled external tools are not registered: {sorted(unknown)}"
            )
        for external in self._external_tools.values():
            if configured_external and external.name not in configured_external:
                continue
            if request.activity.value not in self._validated_scopes(external):
                continue
            tools.append(external)
        names = [value.name for value in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique after composition")
        if "finish_task" in names:
            raise ValueError("ToolProvider cannot provide reserved tool finish_task")
        return ToolSet(
            tools=tools,
            names=frozenset(names),
            awrap_tool_call=self._wrap_tool_call,
        )

    def _device_tools(self, request: ToolBuildRequest) -> list[BaseTool]:
        tools: list[BaseTool] = []
        device = request.device
        if request.activity == TaskType.ACT and request.capabilities.supported_keys:
            key_values = tuple(key.value for key in request.capabilities.supported_keys)
            key_type = Literal.__getitem__(key_values)
            press_args = create_model("PressKeyArgs", key=(key_type, ...))

            @tool(
                args_schema=press_args,
                response_format="content_and_artifact",
            )
            async def device_press_key(key: str) -> tuple[str, dict[str, Any]]:
                """Press one supported Android TV remote key."""
                result = await device.press(RemoteKey(key))
                return result.summary or f"Pressed {key}", result.model_dump(mode="json")

            device_press_key.metadata = {
                "atv.source": "builtin",
                "atv.scopes": ["act"],
            }
            tools.append(device_press_key)
            if request.capabilities.input_text:
                input_args = create_model(
                    "InputTextArgs",
                    text=(str, Field(min_length=1, max_length=1000)),
                )

                @tool(
                    args_schema=input_args,
                    response_format="content_and_artifact",
                )
                async def device_input_text(
                    text: str,
                ) -> tuple[str, dict[str, Any]]:
                    """Enter text into the currently focused TV input."""
                    result = await device.input_text(text)
                    return result.summary or "Entered text", result.model_dump(mode="json")

                device_input_text.metadata = {
                    "atv.source": "builtin",
                    "atv.scopes": ["act"],
                }
                tools.append(device_input_text)

        wait_args = create_model(
            "WaitArgs",
            duration_ms=(int, Field(ge=100, le=10_000)),
        )

        @tool(
            args_schema=wait_args,
            response_format="content_and_artifact",
        )
        async def device_wait(duration_ms: int) -> tuple[str, dict[str, Any]]:
            """Wait briefly without changing the tested business state."""
            result = await device.wait(duration_ms)
            return result.summary or f"Waited {duration_ms}ms", result.model_dump(
                mode="json"
            )

        device_wait.metadata = {
            "atv.source": "builtin",
            "atv.scopes": ["act", "judge"],
        }
        tools.append(device_wait)
        return tools

    def _validated_scopes(self, value: BaseTool) -> frozenset[str]:
        metadata = value.metadata or {}
        raw = metadata.get("atv.scopes", [TaskType.ACT.value])
        scopes = [raw] if isinstance(raw, str) else list(raw)
        allowed = {item.value for item in TaskType}
        if not scopes or any(scope not in allowed for scope in scopes):
            raise ValueError(
                f"Tool {value.name} has invalid atv.scopes: {scopes}"
            )
        return frozenset(scopes)

    async def _wrap_tool_call(
        self,
        request: ToolCallRequest,
        execute: ToolExecutor,
    ) -> ToolMessage:
        metadata = request.tool.metadata if request.tool is not None else {}
        raw_timeout = (metadata or {}).get(
            "atv.timeout_seconds", self.action_timeout_seconds
        )
        timeout = float(raw_timeout)
        if timeout <= 0:
            return self._result_message(
                request,
                ToolExecutionResult(
                    status=ToolExecutionStatus.BLOCKED,
                    summary="Tool timeout must be greater than zero",
                ),
            )

        last_transient: Exception | None = None
        for _ in range(3):
            try:
                response = await asyncio.wait_for(execute(request), timeout=timeout)
                if not isinstance(response, ToolMessage):
                    return self._result_message(
                        request,
                        ToolExecutionResult(
                            status=ToolExecutionStatus.BLOCKED,
                            summary="Tool Command results are not supported",
                        ),
                    )
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.SUCCEEDED,
                    summary=self._content_text(response.content),
                    data=self._result_data(response),
                )
                return self._result_message(request, result)
            except TransientToolError as exc:
                last_transient = exc
                continue
            except (TimeoutError, ActionTimeout) as exc:
                return self._result_message(
                    request,
                    ToolExecutionResult(
                        status=ToolExecutionStatus.TIMED_OUT,
                        summary=str(exc) or f"{request.tool_call['name']} timed out",
                    ),
                )
            except (ToolInvocationError, ValueError, TypeError) as exc:
                return self._result_message(
                    request,
                    ToolExecutionResult(
                        status=ToolExecutionStatus.INVALID,
                        summary=str(exc),
                    ),
                )
            except Exception as exc:
                return self._result_message(
                    request,
                    ToolExecutionResult(
                        status=ToolExecutionStatus.BLOCKED,
                        summary=str(exc) or type(exc).__name__,
                    ),
                )
        return self._result_message(
            request,
            ToolExecutionResult(
                status=ToolExecutionStatus.BLOCKED,
                summary=str(last_transient) if last_transient else "Tool failed",
            ),
        )

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content or "Tool completed"
        return json.dumps(content, ensure_ascii=False, default=str)

    @staticmethod
    def _result_data(message: ToolMessage) -> dict[str, Any]:
        value = message.artifact if message.artifact is not None else message.content
        if isinstance(value, dict):
            return value
        try:
            serialized = json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            serialized = str(value)
        return {"output": serialized}

    @staticmethod
    def _result_message(
        request: ToolCallRequest,
        result: ToolExecutionResult,
    ) -> ToolMessage:
        return ToolMessage(
            content=result.summary,
            artifact=result.model_dump(mode="json"),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status=(
                "success"
                if result.status == ToolExecutionStatus.SUCCEEDED
                else "error"
            ),
        )
