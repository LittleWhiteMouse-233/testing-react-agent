"""Immutable, credential-free tool definitions used by previews and run snapshots."""
from collections import Counter
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ToolAnnotationsSnapshot(BaseModel):
    """Standard MCP annotation facts retained for audit, never used as permissions."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    title: str | None = None
    readOnlyHint: bool | None = Field(default=None, validation_alias=AliasChoices("readOnlyHint", "read_only_hint"))
    destructiveHint: bool | None = Field(default=None, validation_alias=AliasChoices("destructiveHint", "destructive_hint"))
    idempotentHint: bool | None = Field(default=None, validation_alias=AliasChoices("idempotentHint", "idempotent_hint"))
    openWorldHint: bool | None = Field(default=None, validation_alias=AliasChoices("openWorldHint", "open_world_hint"))


class ToolDefinitionSnapshot(BaseModel):
    """One model-visible tool definition with its MCP or built-in source."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]
    source_id: str = Field(min_length=1)
    annotations: ToolAnnotationsSnapshot


class ToolCatalogSnapshot(BaseModel):
    """The complete TaskAgent tool catalog frozen for one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tools: list[ToolDefinitionSnapshot]

    @model_validator(mode="after")
    def tool_names_are_unique(self) -> "ToolCatalogSnapshot":
        name_counts = Counter(tool.name for tool in self.tools)
        collisions = sorted(name for name, count in name_counts.items() if count > 1)
        if collisions:
            raise ValueError(f"Tool names collide: {', '.join(collisions)}")
        return self
