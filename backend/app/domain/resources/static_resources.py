"""Credential-free static resource facts shared by runtime snapshots and listings."""
from pydantic import BaseModel, ConfigDict, Field


class ResourceDefinitionSnapshot(BaseModel):
    """One concrete resource address, readable without template expansion."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None
    is_skill_manifest: bool = False


class ResourceTemplateSnapshot(BaseModel):
    """A URI pattern needing expansion, distinct from a concrete resource.

    Similar descriptive fields belong to different MCP protocol entities;
    preserving that boundary prevents passing an unexpanded pattern to read.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    uri_template: str
    name: str
    description: str | None = None
    mime_type: str | None = None


class ResourceCatalogSnapshot(BaseModel):
    """One metadata catalog; package relationships and initial summaries are derived."""
    # Dumps always include both collections, including an empty catalog. Expose
    # that guarantee in output schemas instead of making API consumers guess.
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)
    local_root: str | None = None
    resources: list[ResourceDefinitionSnapshot] = Field(default_factory=list)
    templates: list[ResourceTemplateSnapshot] = Field(default_factory=list)
