"""Agent 外接资源在领域内的框架无关表示。

LLM 集成与外部 MCP 工具拥有运行实现；本包只保存可进入 API、
快照与规划上下文的 canonical 事实，不包含设备实体。
"""

from app.domain.resources.llm import LLMProfileSettings
from app.domain.resources.static_resources import (
    ResourceCatalogSnapshot,
    ResourceDefinitionSnapshot,
    ResourceTemplateSnapshot,
    is_skill_uri,
    is_resource_in_skill_package,
    top_level_skills,
)
from app.domain.resources.capabilities import (
    CapabilityCatalog, CapabilityCatalogChanged, CapabilityColumnSelection, CapabilityColumnSnapshot,
    LOCAL_OTHER_COLUMN_ID, build_capability_preview, select_capabilities,
)
from app.domain.resources.tools import (
    ToolAnnotationsSnapshot,
    ToolCatalogSnapshot,
    ToolDefinitionSnapshot,
)

__all__ = [
    "LLMProfileSettings",
    "ResourceCatalogSnapshot",
    "ResourceDefinitionSnapshot",
    "ResourceTemplateSnapshot",
    "ToolAnnotationsSnapshot",
    "ToolCatalogSnapshot",
    "ToolDefinitionSnapshot",
    "CapabilityCatalog", "CapabilityCatalogChanged", "CapabilityColumnSelection", "CapabilityColumnSnapshot",
    "LOCAL_OTHER_COLUMN_ID", "build_capability_preview", "select_capabilities",
    "is_skill_uri", "is_resource_in_skill_package", "top_level_skills",
]
