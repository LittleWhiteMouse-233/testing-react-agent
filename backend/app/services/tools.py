from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jsonschema import ValidationError, validate

from app.domain.errors import InvalidArguments, UnsupportedAction
from app.domain.tools import (
    ToolContext,
    ToolDefinition,
    ToolResult,
)

ToolHandler = Callable[[dict[str, object], ToolContext], Awaitable[ToolResult]]


class ToolProvider(Protocol):
    def list_tools(self) -> list[ToolDefinition]: ...

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> ToolResult: ...


class StaticToolProvider:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDefinition, ToolHandler]] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        self._tools[definition.name] = (definition, handler)

    def list_tools(self) -> list[ToolDefinition]:
        return [item[0] for item in self._tools.values()]

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> ToolResult:
        item = self._tools.get(tool_name)
        if not item:
            raise UnsupportedAction(f"Tool is not enabled: {tool_name}")
        definition, handler = item
        try:
            validate(arguments, definition.input_schema)
        except ValidationError as exc:
            raise InvalidArguments(exc.message) from exc
        return await asyncio.wait_for(
            handler(arguments, context), timeout=definition.timeout_seconds
        )
