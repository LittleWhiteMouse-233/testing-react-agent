"""LLM profile 的固定领域快照；公开序列化排除令牌。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import ModelProfileId


class LLMProfileSnapshot(BaseModel):
    """冻结实际模型选择，内存保留认证令牌，持久化及公开输出排除令牌。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: ModelProfileId
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str | None
    temperature: float
    timeout_seconds: float = Field(gt=0)
    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    characters_per_token: float = Field(gt=0)
    tokens_per_image: int = Field(gt=0)
    context_safety_margin_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def input_budget_is_positive(self) -> "LLMProfileSnapshot":
        reserved = self.max_output_tokens + self.context_safety_margin_tokens
        if reserved >= self.context_window_tokens:
            raise ValueError(
                "max_output_tokens plus context_safety_margin_tokens must be "
                "smaller than context_window_tokens"
            )
        return self
