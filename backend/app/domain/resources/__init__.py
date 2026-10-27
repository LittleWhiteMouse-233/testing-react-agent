"""Agent 外接资源在领域内的框架无关表示。

LLM 集成与外部 MCP 工具拥有运行实现；本包只保存可进入 API、
快照与规划上下文的 canonical 事实，不包含设备实体。
"""

from app.domain.resources.llm import LLMProfileSnapshot
from app.domain.resources.static_resources import (
    ResourceCatalogSnapshot,
    ResourceDefinitionSnapshot,
    ResourceTemplateSnapshot,
)
from app.domain.resources.tools import (
    ToolAnnotationsSnapshot,
    ToolCatalogSnapshot,
    ToolDefinitionSnapshot,
)

__all__ = [
    "LLMProfileSnapshot",
    "ResourceCatalogSnapshot",
    "ResourceDefinitionSnapshot",
    "ResourceTemplateSnapshot",
    "ToolAnnotationsSnapshot",
    "ToolCatalogSnapshot",
    "ToolDefinitionSnapshot",
]
