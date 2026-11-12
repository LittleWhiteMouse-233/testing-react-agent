"""Capability columns own preview consistency and each run's access decisions."""
from __future__ import annotations

from collections.abc import Collection, Sequence
import hashlib
import json
from pathlib import Path
from typing import Generic, TypeVar
from urllib.parse import urlsplit
from urllib.request import url2pathname

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.resources.static_resources import ResourceCatalogSnapshot, top_level_skills, is_resource_in_skill_package
from app.domain.resources.tools import ToolCatalogSnapshot


LOCAL_OTHER_COLUMN_ID = "local:other"


class CapabilityColumnSelection(BaseModel):
    """API consistency token: identifies one selected column and its preview definition."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityColumnSnapshot(BaseModel):
    """Persisted access decision for one discovered column, including unselected ones."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    selected: bool


ColumnT = TypeVar("ColumnT", CapabilityColumnSelection, CapabilityColumnSnapshot)


class CapabilityCatalog(BaseModel, Generic[ColumnT]):
    """One full capability family; columns coordinate preview or immutable run decisions."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_catalog: ToolCatalogSnapshot
    resource_catalog: ResourceCatalogSnapshot
    columns: list[ColumnT]

    @model_validator(mode="after")
    def column_ids_are_unique(self) -> CapabilityCatalog[ColumnT]:
        """A column has exactly one preview token or run access decision."""
        if len({column.id for column in self.columns}) != len(self.columns):
            raise ValueError("Capability column ids must be unique")
        return self

    def resource_column_id(self, source_id: str, uri: str) -> str:
        """Classify an address against all known local packages, never only selected ones."""
        if source_id != "local":
            return source_id
        for column in self.columns:
            if column.id.startswith("file:") and is_resource_in_skill_package(column.id, uri):
                return column.id
        return LOCAL_OTHER_COLUMN_ID

    def column_is_selected(self, column_id: str) -> bool:
        """Only run snapshots grant access; preview tokens do not grant permissions."""
        return any(isinstance(column, CapabilityColumnSnapshot) and column.selected and column.id == column_id
                   for column in self.columns)

    def resource_is_available(self, source_id: str, uri: str) -> bool:
        """Authorize by column; catalogue enumeration is not a URI whitelist."""
        if source_id == "local":
            root = self.resource_catalog.local_root
            if root is None or not Path(url2pathname(urlsplit(uri).path)).is_relative_to(Path(root)):
                return False
        return self.column_is_selected(self.resource_column_id(source_id, uri))

    def tool_is_available(self, source_id: str) -> bool:
        """Built-in tools are always available; external tools follow their MCP column."""
        return source_id.startswith("builtin:") or self.column_is_selected(source_id)

    def visible_resources(self) -> ResourceCatalogSnapshot:
        """Derive model-visible metadata without copying it into another persisted catalogue."""
        return ResourceCatalogSnapshot(
            local_root=self.resource_catalog.local_root,
            resources=[resource for resource in self.resource_catalog.resources
                       if self.resource_is_available(resource.source_id, resource.uri)],
            templates=[template for template in self.resource_catalog.templates if self.column_is_selected(template.source_id)],
        )


class CapabilityCatalogChanged(ValueError):
    """A selected preview column disappeared or changed before run preparation finished."""


def build_capability_preview(
    tool_catalog: ToolCatalogSnapshot, resource_catalog: ResourceCatalogSnapshot, mcp_source_ids: Sequence[str],
) -> CapabilityCatalog[CapabilityColumnSelection]:
    """Build stable per-column fingerprints from directory facts, excluding resource bodies."""
    column_ids = [*sorted(mcp_source_ids),
                  *sorted(skill.uri for skill in top_level_skills(resource_catalog) if skill.source_id == "local"),
                  LOCAL_OTHER_COLUMN_ID]
    classification = CapabilityCatalog[CapabilityColumnSnapshot](
        tool_catalog=tool_catalog, resource_catalog=resource_catalog,
        columns=[CapabilityColumnSnapshot(id=column_id, selected=False) for column_id in column_ids],
    )
    columns: list[CapabilityColumnSelection] = []
    for column_id in column_ids:
        resources = sorted((resource for resource in resource_catalog.resources
                            if classification.resource_column_id(resource.source_id, resource.uri) == column_id),
                           key=lambda resource: (resource.source_id, resource.uri))
        templates = sorted((template for template in resource_catalog.templates if template.source_id == column_id),
                           key=lambda template: template.uri_template)
        tools = sorted((tool for tool in tool_catalog.tools if tool.source_id == column_id), key=lambda tool: tool.name)
        definition = {
            "id": column_id,
            "local_root": resource_catalog.local_root if not column_id.startswith("mcp:") else None,
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "resources": [resource.model_dump(mode="json") for resource in resources],
            "templates": [template.model_dump(mode="json") for template in templates],
        }
        fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        columns.append(CapabilityColumnSelection(id=column_id, fingerprint=fingerprint))
    return CapabilityCatalog[CapabilityColumnSelection](
        tool_catalog=tool_catalog, resource_catalog=resource_catalog, columns=columns,
    )


def merge_capability_preview(
    cached_preview: CapabilityCatalog[CapabilityColumnSelection],
    discovered_preview: CapabilityCatalog[CapabilityColumnSelection],
    refreshed_mcp_source_ids: Collection[str],
) -> CapabilityCatalog[CapabilityColumnSelection]:
    """Replace requested MCP sources and all local facts; retain other cached sources."""
    retained_source_ids = {
        column.id for column in cached_preview.columns
        if column.id.startswith("mcp:") and column.id not in refreshed_mcp_source_ids
    }
    tool_catalog = ToolCatalogSnapshot(tools=[
        *(tool for tool in cached_preview.tool_catalog.tools if tool.source_id in retained_source_ids),
        *discovered_preview.tool_catalog.tools,
    ])
    resource_catalog = ResourceCatalogSnapshot(
        local_root=discovered_preview.resource_catalog.local_root,
        resources=[
            *(resource for resource in cached_preview.resource_catalog.resources if resource.source_id in retained_source_ids),
            *discovered_preview.resource_catalog.resources,
        ],
        templates=[
            *(template for template in cached_preview.resource_catalog.templates if template.source_id in retained_source_ids),
            *discovered_preview.resource_catalog.templates,
        ],
    )
    source_ids = [*retained_source_ids, *(column.id for column in discovered_preview.columns if column.id.startswith("mcp:"))]
    return build_capability_preview(tool_catalog, resource_catalog, source_ids)


def select_capabilities(
    preview: CapabilityCatalog[CapabilityColumnSelection], selections: Sequence[CapabilityColumnSelection],
) -> CapabilityCatalog[CapabilityColumnSnapshot]:
    """Validate selected previews and freeze the complete directory view for audit."""
    selected_by_id = {selection.id: selection.fingerprint for selection in selections}
    if len(selected_by_id) != len(selections):
        raise ValueError("Selected capability columns must be unique")
    current_by_id = {column.id: column.fingerprint for column in preview.columns}
    changed = [column_id for column_id, fingerprint in selected_by_id.items()
               if current_by_id.get(column_id) != fingerprint]
    if changed:
        raise CapabilityCatalogChanged("所选栏目不存在或目录已变化，请刷新资源目录后重选：" + ", ".join(changed))
    return CapabilityCatalog[CapabilityColumnSnapshot](
        tool_catalog=preview.tool_catalog, resource_catalog=preview.resource_catalog,
        columns=[CapabilityColumnSnapshot(id=column.id, selected=column.id in selected_by_id) for column in preview.columns],
    )
