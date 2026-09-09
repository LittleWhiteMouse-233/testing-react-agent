"""Immutable, credential-free projections of discovered MCP tools."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolAnnotationsSnapshot(BaseModel):
    """Standard MCP annotation facts retained for audit, never used as permissions."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    title: str | None = None
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None


class ToolCapability(BaseModel):
    """One discovered tool definition, owned by its external MCP server."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]
    source: str = Field(min_length=1)
    annotations: ToolAnnotationsSnapshot


class ToolCatalogSnapshot(BaseModel):
    """The actual discovered tool catalog frozen for one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tools: list[ToolCapability]

    @model_validator(mode="after")
    def tool_names_are_unique(self) -> "ToolCatalogSnapshot":
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return self
