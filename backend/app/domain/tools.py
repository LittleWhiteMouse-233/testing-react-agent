from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.utils.pydantic import model_json_schema
from pydantic import BaseModel, Field


class DeviceHealth(BaseModel):
    available: bool
    message: str = ""


class DeviceDescription(BaseModel):
    model: str | None = None
    resolution: str | None = None
    locale: str | None = None


@dataclass(frozen=True)
class ToolEntry:
    tool: BaseTool
    scopes: frozenset[str]


class ToolCapabilitySnapshot(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    scopes: list[str]


class DeviceCapabilitiesSnapshot(BaseModel):
    device_id: str
    provider: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    tools: list[ToolCapabilitySnapshot] = Field(default_factory=list)


@dataclass(frozen=True)
class DeviceCapabilities:
    device_id: str
    provider: str
    tools: tuple[ToolEntry, ...]
    metadata: dict[str, Any]

    def to_snapshot(self) -> DeviceCapabilitiesSnapshot:
        def input_schema(tool: BaseTool) -> dict[str, Any]:
            schema = tool.tool_call_schema
            return schema if isinstance(schema, dict) else model_json_schema(schema)

        return DeviceCapabilitiesSnapshot(
            device_id=self.device_id,
            provider=self.provider,
            metadata=dict(self.metadata),
            tools=[
                ToolCapabilitySnapshot(
                    name=entry.tool.name,
                    description=entry.tool.description or "",
                    input_schema=input_schema(entry.tool),
                    scopes=sorted(entry.scopes),
                )
                for entry in self.tools
            ],
        )


class ScreenshotData(BaseModel):
    content: bytes
    mime_type: str = "image/png"
    activity: str | None = None


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
