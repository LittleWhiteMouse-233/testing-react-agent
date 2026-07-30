from __future__ import annotations

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel


class ChatModelProvider(Protocol):
    model_info: dict[str, object]

    def create_model(self) -> BaseChatModel: ...
