from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


class RealChatModelProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0,
        timeout_seconds: float = 60,
        save_raw_response: bool = False,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("LLM_BASE_URL and LLM_API_KEY are required in real mode")
        self._settings = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "temperature": temperature,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        self.save_raw_response = save_raw_response
        self.model_info: dict[str, object] = {
            "provider": "openai-compatible",
            "base_url": base_url,
            "model": model,
            "temperature": temperature,
        }

    def create_model(self) -> BaseChatModel:
        return ChatOpenAI(**self._settings)
