from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import LLMProfileSettings
from app.llm.snapshots import profile_snapshot_from_settings


class RealChatModelProvider:
    def __init__(
        self,
        settings: LLMProfileSettings,
    ) -> None:
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
        }
        self.profile_snapshot = profile_snapshot_from_settings(settings)

    def create_model(self) -> BaseChatModel:
        return ChatOpenAI(**self._settings)
