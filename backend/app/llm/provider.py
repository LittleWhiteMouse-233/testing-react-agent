"""按本轮固定模型快照构建客户端，不持有配置路由或客户端缓存。"""
from __future__ import annotations

from app.config import LLMProfileSettings
from app.domain.resources import LLMProfileSnapshot
from app.llm.client import ChatModelClient, RealChatModelClient
from app.llm.test_fake import ScriptedChatModelClient


class ModelProvider:
    """在完整快照与客户端构造之间集中转换部署表示。"""

    def client_for_profile(self, profile: LLMProfileSnapshot) -> ChatModelClient:
        """只使用传入快照；已开始的工作不重新查询当前模型配置。"""
        settings = LLMProfileSettings.model_validate({
            **profile.model_dump(exclude={"profile_id", "provider"}),
            "id": profile.profile_id,
            "mode": "real" if profile.provider == "openai-compatible" else "scripted",
            "api_key": profile.api_key,
        })
        return RealChatModelClient(settings) if settings.mode == "real" else ScriptedChatModelClient(settings)
