"""Credential-free static resource facts shared by runtime snapshots and listings."""
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

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


def is_skill_uri(source_id: str, uri: str) -> bool:
    """Recognize local and MCP Skill entry addresses without reading their content."""
    try:
        address = urlsplit(uri)
    except ValueError:
        return False
    return address.path.rsplit("/", 1)[-1] == "SKILL.md" and (
        address.scheme == "file" if source_id == "local" else address.scheme == "skill"
    )


def is_resource_in_skill_package(skill_uri: str, resource_uri: str) -> bool:
    """Determine package membership, respecting filesystem path semantics locally."""
    skill_address, resource_address = urlsplit(skill_uri), urlsplit(resource_uri)
    if (skill_address.scheme, skill_address.netloc) != (resource_address.scheme, resource_address.netloc):
        return False
    if skill_address.scheme == "file":
        package_root = Path(url2pathname(skill_address.path)).parent
        return Path(url2pathname(resource_address.path)).is_relative_to(package_root)
    return resource_address.path.startswith(skill_address.path.rsplit("/", 1)[0] + "/")


def top_level_skills(catalog: ResourceCatalogSnapshot) -> list[ResourceDefinitionSnapshot]:
    """Derive Skill roots from the complete source-bound resource catalogue."""
    candidates = [resource for resource in catalog.resources if is_skill_uri(resource.source_id, resource.uri)]
    return [resource for resource in candidates if not any(
        ancestor.source_id == resource.source_id
        and len(urlsplit(ancestor.uri).path) < len(urlsplit(resource.uri).path)
        and is_resource_in_skill_package(ancestor.uri, resource.uri)
        for ancestor in candidates
    )]
