"""固定 LLM profile 的客户端合同、构造入口与真实实现。"""

from __future__ import annotations

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.domain.resources import LLMProfileSettings
from app.llm.test_fake import ScriptedChatModelClient


class ChatModelClient(Protocol):
    """绑定一个固定 profile 的模型访问对象；配置路由在工作准备时选择。"""

    profile_snapshot: LLMProfileSettings

    @property
    def model_id(self) -> str: ...

    @property
    def timeout_seconds(self) -> float: ...

    def create_model(self) -> BaseChatModel: ...


def create_model_client(profile: LLMProfileSettings) -> ChatModelClient:
    """只使用准备阶段固定的 profile 构造客户端，不重新查询配置。"""

    return RealChatModelClient(profile) if profile.mode == "real" else ScriptedChatModelClient(profile)


class RealChatModelClient:
    """一个 OpenAI-compatible profile 的配置化 LangChain 客户端。"""

    def __init__(self, profile: LLMProfileSettings) -> None:
        self.profile_snapshot = profile

    @property
    def model_id(self) -> str:
        return self.profile_snapshot.id

    @property
    def timeout_seconds(self) -> float:
        return self.profile_snapshot.timeout_seconds

    def create_model(self) -> BaseChatModel:
        profile = self.profile_snapshot
        return ChatOpenAI(**{
            "base_url": profile.base_url,
            "api_key": profile.api_key,
            "model": profile.model,
            "temperature": profile.temperature,
            "timeout": profile.timeout_seconds,
            "max_retries": 0,
            "max_tokens": profile.max_output_tokens,
        })
