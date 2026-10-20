"""Replay standard MCP tool calls and responses without application-specific concepts."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate
from mcp import MCPError
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp_types import (
    BlobResourceContents, CallToolRequestParams, CallToolResult, ListResourcesResult,
    ListResourceTemplatesResult, ListToolsResult, ReadResourceRequestParams,
    ReadResourceResult, Resource, ResourceTemplate, TextContent, TextResourceContents, Tool,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ReplayCall(BaseModel):
    """One expected protocol request and its deterministic outcome."""

    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    result: CallToolResult
    delay_seconds: float = Field(default=0, ge=0)
    disconnect: bool = False
    cooperative_cancellation: bool = True


class ReplayScenario(BaseModel):
    """Tool discovery and ordered responses across framework-owned sessions."""

    model_config = ConfigDict(extra="forbid")
    tools: list[Tool]
    calls: list[ReplayCall]
    startup_delay_seconds: float = Field(default=0, ge=0)
    resources: list[Resource] = Field(default_factory=list)
    resource_templates: list[ResourceTemplate] = Field(default_factory=list)
    resource_contents: dict[str, list[TextResourceContents | BlobResourceContents]] = Field(default_factory=dict)
    resource_disconnects: list[str] = Field(default_factory=list)


async def serve(scenario_path: Path, record_path: Path) -> None:
    scenario = ReplayScenario.model_validate_json(scenario_path.read_text(encoding="utf-8"))
    next_call = sum(
        json.loads(line)["phase"] == "started"
        for line in record_path.read_text(encoding="utf-8").splitlines()
    ) if record_path.exists() else 0

    def record(phase: str, **facts: JsonValue) -> None:
        with record_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": phase, "pid": os.getpid(), "time_seconds": time.perf_counter(), **facts}) + "\n")

    async def list_tools(context: Any, params: Any) -> ListToolsResult:
        record("discovery")
        return ListToolsResult(tools=scenario.tools)

    async def call_tool(context: Any, params: CallToolRequestParams) -> CallToolResult:
        nonlocal next_call
        name, arguments = params.name, params.arguments or {}
        # SDK 2's low-level callback dispatch does not validate tool schemas.
        # Reject malformed requests before they consume a replay operation.
        tool = next((entry for entry in scenario.tools if entry.name == name), None)
        if tool is None:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=f"Unknown tool: {name}")])
        try:
            validate(arguments, tool.input_schema)
        except ValidationError as exc:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=exc.message)])
        index = next_call
        next_call += 1
        record("started", name=name, arguments=arguments, index=index)
        if index >= len(scenario.calls):
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Replay exhausted")])
        expected = scenario.calls[index]
        if name != expected.name or arguments != expected.arguments:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=f"Unexpected call at index {index}")])
        if expected.disconnect:
            record("disconnected", index=index)
            os._exit(23)
        try:
            if expected.cooperative_cancellation:
                await asyncio.sleep(expected.delay_seconds)
            else:
                # Simulate a server operation that cannot observe cancellation.
                time.sleep(expected.delay_seconds)
            record("completed", index=index)
            return expected.result
        except asyncio.CancelledError:
            record("cancelled", index=index)
            raise

    async def list_resources(context: Any, params: Any) -> ListResourcesResult:
        record("resource.list")
        return ListResourcesResult(resources=scenario.resources)

    async def list_templates(context: Any, params: Any) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resource_templates=scenario.resource_templates)

    async def read_resource(context: Any, params: ReadResourceRequestParams) -> ReadResourceResult:
        uri = str(params.uri)
        record("resource.read", uri=uri)
        if uri in scenario.resource_disconnects:
            os._exit(23)
        if uri not in scenario.resource_contents:
            raise MCPError(-32002, f"Resource not found: {uri}")
        return ReadResourceResult(contents=scenario.resource_contents[uri])

    has_resources = bool(scenario.resources or scenario.resource_templates or scenario.resource_contents or scenario.resource_disconnects)
    server = Server(
        "scripted-mcp", version="0.1.0",
        on_list_tools=list_tools if scenario.tools else None,
        on_call_tool=call_tool if scenario.tools else None,
        on_list_resources=list_resources if has_resources else None,
        on_list_resource_templates=list_templates if has_resources else None,
        on_read_resource=read_resource if has_resources else None,
    )
    record("opened")
    try:
        await asyncio.sleep(scenario.startup_delay_seconds)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        record("closed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    options = parser.parse_args()
    asyncio.run(serve(options.scenario, options.record))


if __name__ == "__main__":
    main()
