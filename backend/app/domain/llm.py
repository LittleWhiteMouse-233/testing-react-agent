from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ids import ModelProfileId


class LLMProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: ModelProfileId
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str | None
    temperature: float
    timeout_seconds: float = Field(gt=0)
