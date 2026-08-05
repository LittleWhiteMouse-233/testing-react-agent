from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.domain.execution import ModelSnapshot


class RealChatModelProvider:
    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0,
        timeout_seconds: float = 60,
        save_raw_response: bool = False,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required for real model profiles")
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds
        self._settings = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "temperature": temperature,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        self.save_raw_response = save_raw_response
        self.model_snapshot = ModelSnapshot(
            profile_id=model_id,
            provider="openai-compatible",
            model=model,
            base_url=base_url,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

    def create_model(self) -> BaseChatModel:
        return ChatOpenAI(**self._settings)
