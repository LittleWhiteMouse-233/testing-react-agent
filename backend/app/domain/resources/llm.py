"""配置、客户端和工作快照共用的 LLM profile；默认序列化排除令牌。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import ModelProfileId


class LLMProfileSettings(BaseModel):
    """不可变模型部署参数；工作准备时固定副本，公开输出排除令牌。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: ModelProfileId = Field(title="模型配置 ID", description="唯一、稳定的模型配置标识；修改 ID 视为新增模型。")
    mode: Literal["scripted", "real"] = Field(title="模型模式", description="real 为真实服务，scripted 仅用于可控测试。")
    base_url: str | None = Field(default=None, title="服务地址", description="真实模型的 OpenAI 兼容 API 基地址。")
    api_key: str | None = Field(default=None, exclude=True, repr=False, title="模型令牌", description="留空保留已有令牌；新真实模型必须填写。", json_schema_extra={"writeOnly": True, "format": "password"})
    model: str = Field(min_length=1, title="模型名称", description="服务端实际使用的模型名称。")
    temperature: float = Field(title="生成温度", description="模型服务的 temperature 请求参数。")
    timeout_seconds: float = Field(gt=0, title="模型请求超时", description="每次模型调用的超时秒数。")
    context_window_tokens: int = Field(ge=2_048, title="上下文容量", description="模型完整上下文的 token 容量。")
    max_output_tokens: int = Field(ge=1, title="最大输出容量", description="为模型输出预留的 token 数。")
    characters_per_token: float = Field(gt=0, title="每 token 字符数", description="文字容量估算使用的每 token 字符数。")
    tokens_per_image: int = Field(ge=1, title="每张图片容量", description="每张图片估算占用的 token 数。")
    context_safety_margin_tokens: int = Field(ge=0, title="上下文安全余量", description="额外预留的 token 数。")

    @model_validator(mode="after")
    def model_parameters_are_valid(self) -> LLMProfileSettings:
        if self.mode == "real" and not self.base_url:
            raise ValueError("base_url is required for real model profiles")
        if self.max_output_tokens + self.context_safety_margin_tokens >= self.context_window_tokens:
            raise ValueError("max_output_tokens plus context_safety_margin_tokens must be smaller than context_window_tokens")
        return self
