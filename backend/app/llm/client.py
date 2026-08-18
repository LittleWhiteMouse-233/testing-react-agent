"""单个 LLM profile 的客户端合同、真实实现与脱敏快照转换。"""

from __future__ import annotations

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import LLMProfileSettings
from app.domain.resources.llm import LLMProfileSnapshot


class ChatModelClient(Protocol):
    """绑定一个 profile 的模型访问对象；activity 选择由 ModelProvider 负责。"""

    model_id: str
    profile_snapshot: LLMProfileSnapshot
    timeout_seconds: float

    def create_model(self) -> BaseChatModel: ...


def profile_snapshot_from_settings(
    settings: LLMProfileSettings,
) -> LLMProfileSnapshot:
    """唯一的部署配置到公开审计快照单向脱敏转换。"""

    provider = "openai-compatible" if settings.mode == "real" else "scripted"
    return LLMProfileSnapshot(
        profile_id=settings.id,
        provider=provider,
        model=settings.model,
        base_url=settings.base_url if settings.mode == "real" else None,
        temperature=settings.temperature,
        timeout_seconds=settings.timeout_seconds,
        context_window_tokens=settings.context_window_tokens,
        max_output_tokens=settings.max_output_tokens,
        characters_per_token=settings.characters_per_token,
        tokens_per_image=settings.tokens_per_image,
        context_safety_margin_tokens=settings.context_safety_margin_tokens,
    )


class RealChatModelClient:
    """一个 OpenAI-compatible profile 的配置化 LangChain 客户端。"""

    def __init__(self, settings: LLMProfileSettings) -> None:
        if not settings.base_url or not settings.api_key:
            raise ValueError("base_url and api_key are required for real model profiles")
        self.model_id = settings.id
        self.timeout_seconds = settings.timeout_seconds
        self._settings = {
            "base_url": settings.base_url,
            "api_key": settings.api_key,
            "model": settings.model,
            "temperature": settings.temperature,
            "timeout": settings.timeout_seconds,
            "max_retries": 0,
            "max_tokens": settings.max_output_tokens,
        }
        self.profile_snapshot = profile_snapshot_from_settings(settings)

    def create_model(self) -> BaseChatModel:
        return ChatOpenAI(**self._settings)
