from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.activity import AgentActivity


class ToolCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]
    scopes: list[AgentActivity] = Field(min_length=1)
    changes_device_state: bool

    @model_validator(mode="after")
    def scopes_are_valid(self) -> "ToolCapability":
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("tool scopes must be unique")
        return self


class ToolCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: list[ToolCapability]

    @model_validator(mode="after")
    def tool_names_are_unique(self) -> "ToolCatalogSnapshot":
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool catalog names must be unique")
        return self
