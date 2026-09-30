"""Replay standard MCP tool calls and responses without application-specific concepts."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
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
    """Tool discovery and ordered protocol responses for one server session."""

    model_config = ConfigDict(extra="forbid")
    tools: list[Tool]
    calls: list[ReplayCall]
    startup_delay_seconds: float = Field(default=0, ge=0)


async def serve(scenario_path: Path, record_path: Path) -> None:
    scenario = ReplayScenario.model_validate_json(scenario_path.read_text(encoding="utf-8"))
    server = Server("scripted-mcp", version="0.1.0")
    next_call = 0

    def record(phase: str, **facts: JsonValue) -> None:
        with record_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": phase, "pid": os.getpid(), "time_seconds": time.perf_counter(), **facts}) + "\n")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        record("discovery")
        return scenario.tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        nonlocal next_call
        index = next_call
        next_call += 1
        record("started", name=name, arguments=arguments, index=index)
        if index >= len(scenario.calls):
            return CallToolResult(isError=True, content=[TextContent(type="text", text="Replay exhausted")])
        expected = scenario.calls[index]
        if name != expected.name or arguments != expected.arguments:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=f"Unexpected call at index {index}")])
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
