from __future__ import annotations

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from app.domain.llm import LLMProfileSnapshot


class ChatModelProvider(Protocol):
    model_id: str
    profile_snapshot: LLMProfileSnapshot
    timeout_seconds: float

    def create_model(self) -> BaseChatModel: ...
