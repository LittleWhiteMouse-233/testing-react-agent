from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from app.domain.execution import ModelSnapshot


class ModelActivity(StrEnum):
    PLANNING = "planning"
    ACT = "act"
    JUDGE = "judge"


class ChatModelProvider(Protocol):
    model_id: str
    model_snapshot: ModelSnapshot
    timeout_seconds: float

    def create_model(self) -> BaseChatModel: ...
