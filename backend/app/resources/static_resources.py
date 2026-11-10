"""Progressively disclose local Skills and source-bound MCP static resources."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import url2pathname

import frontmatter
from fastmcp import Client
from fastmcp.utilities.skills import get_skill_manifest
from langchain_core.messages.content import ImageContentBlock, TextContentBlock, create_image_block, create_text_block
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.prebuilt import ToolRuntime
from mcp import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT, BlobResourceContents, TextResourceContents
from pydantic import AnyUrl
from rfc3986 import uri_reference
from rfc3986.exceptions import ResolutionError

from app.domain.resources import (
    ResourceCatalogSnapshot, ResourceDefinitionSnapshot, ResourceTemplateSnapshot,
    CapabilityCatalog, CapabilityColumnSnapshot,
    is_skill_uri as _is_skill_uri, is_resource_in_skill_package as _is_resource_in_skill_package,
    top_level_skills,
)
from app.resources.mcp import MCPClientProvider


ResourceContent = TextContentBlock | ImageContentBlock
LOAD_SKILL_TOOL_NAME = "load_skill"
_TEXT_APPLICATION_MIMES = {"application/json", "application/xml", "application/yaml", "application/x-yaml", "application/toml", "application/javascript"}


def _normalize_mime_type(mime_type: str | None) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def _is_text_mime(mime_type: str | None) -> bool:
    media_type = _normalize_mime_type(mime_type)
    return media_type.startswith("text/") or media_type in _TEXT_APPLICATION_MIMES


def _is_unsupported_mime(mime_type: str | None) -> bool:
    media_type = _normalize_mime_type(mime_type)
    return bool(media_type and not (_is_text_mime(media_type) or media_type.startswith("image/")))


@runtime_checkable
class ResourceToolContext(Protocol):
    """Framework injection contract; resource tools need no execution-layer imports."""

    @property
    def capability_catalog(self) -> CapabilityCatalog[CapabilityColumnSnapshot]:
        """The run's frozen catalogue, independent of TaskAgent implementation types."""
        ...


def _resolve_relative_resource_uri(uri: str, base_uri: str | None = None) -> str:
    reference = uri_reference(uri)
    if reference.scheme:
        return uri
    if not base_uri or not uri_reference(base_uri).scheme:
        raise ToolException("Relative resource uri requires an absolute base_uri (copy the document URI).")
    try:
        return reference.resolve_with(base_uri).unsplit()
    except (ResolutionError, ValueError) as exc:
        raise ToolException(f"Invalid resource URI or base_uri: {exc}") from exc


class SkillResourceProvider:
    """Discover each run's metadata catalog; read content on demand."""

    def __init__(self, local_root: Path, mcp_clients: MCPClientProvider) -> None:
        self.local_root = local_root.resolve()
        self.mcp_clients = mcp_clients

    def _scan_local_resource(self) -> list[ResourceDefinitionSnapshot]:
        return [ResourceDefinitionSnapshot(
            source_id="local", uri=path.resolve().as_uri(), name=path.relative_to(self.local_root).as_posix(),
            mime_type=mimetypes.guess_type(path.name)[0],
        ) for path in sorted(self.local_root.rglob("*"))
            if path.is_file() and path.resolve().is_relative_to(self.local_root)]

    def _resolve_local_resource_path(self, uri: str) -> Path:
        try:
            address = urlsplit(uri)
        except ValueError as exc:
            raise ToolException(f"Invalid local resource URI: {exc}") from exc
        if address.scheme != "file" or address.netloc not in {"", "localhost"} or address.query:
            raise ToolException("Local resources require a file URI inside the configured Skill root.")
        try:
            path = Path(url2pathname(address.path)).resolve()
        except (ValueError, OSError) as exc:
            raise ToolException(f"Invalid local resource URI: {exc}") from exc
        if not path.is_relative_to(self.local_root):
            raise ToolException("Resource path escapes the configured Skill root.")
        return path

    def _get_mcp_client(self, source_id: str) -> Client[Any]:
        if not source_id.startswith("mcp:") or source_id[4:] not in self.mcp_clients.clients:
            raise ToolException(f"Unknown resource source: {source_id}")
        return self.mcp_clients.clients[source_id[4:]]

    async def discover_resources(self) -> ResourceCatalogSnapshot:
        resources = await asyncio.to_thread(self._scan_local_resource)
        templates: list[ResourceTemplateSnapshot] = []
        for server_name, client in self.mcp_clients.clients.items():
            source_id = f"mcp:{server_name}"
            async with client:
                if client.server_capabilities is None or client.server_capabilities.resources is None:
                    continue
                for resource in await client.list_resources():
                    fastmcp_metadata = (resource.meta or {}).get("fastmcp")
                    skill_metadata = fastmcp_metadata.get("skill") if isinstance(fastmcp_metadata, dict) else None
                    skill_metadata = skill_metadata if isinstance(skill_metadata, dict) else {}
                    skill_name = skill_metadata.get("name")
                    resources.append(ResourceDefinitionSnapshot(
                        source_id=source_id, uri=str(resource.uri),
                        name=skill_name if _is_skill_uri(source_id, str(resource.uri)) and isinstance(skill_name, str) and skill_name else resource.name,
                        description=resource.description, mime_type=resource.mime_type,
                        is_skill_manifest=skill_metadata.get("is_manifest") is True,
                    ))
                templates.extend(ResourceTemplateSnapshot(
                    source_id=source_id, uri_template=entry.uri_template, name=entry.name,
                    description=entry.description, mime_type=entry.mime_type,
                ) for entry in await client.list_resource_templates())
        expanded = await self._expand_manifests(resources)
        unique = {(resource.source_id, resource.uri): resource for resource in expanded}
        unique.update({(resource.source_id, resource.uri): resource for resource in resources})
        resources = list(unique.values())
        catalog = ResourceCatalogSnapshot(local_root=str(self.local_root), resources=resources, templates=templates)
        summaries: dict[tuple[str, str], ResourceDefinitionSnapshot] = {}
        for entry in top_level_skills(catalog):
            if _is_unsupported_mime(entry.mime_type):
                continue
            if entry.source_id == "local" or not entry.name or not entry.description:
                contents = await self._read_resource_contents(entry.source_id, entry.uri)
                if any(_is_unsupported_mime(content.mime_type) for content in contents):
                    continue
                document = frontmatter.loads("\n".join(content.text for content in contents if isinstance(content, TextResourceContents)))
                name = document.get("name")
                description = document.get("description")
                summaries[(entry.source_id, entry.uri)] = entry.model_copy(update={
                    "name": (name if isinstance(name, str) and name else self._resolve_local_resource_path(entry.uri).parent.name)
                        if entry.source_id == "local" else entry.name or (name if isinstance(name, str) else "SKILL.md"),
                    "description": entry.description or (description if isinstance(description, str) else None),
                })
        return catalog.model_copy(update={"resources": [
            summaries.get((entry.source_id, entry.uri), entry) for entry in resources
        ]})

    async def _expand_manifests(self, resources: list[ResourceDefinitionSnapshot]) -> list[ResourceDefinitionSnapshot]:
        expanded: list[ResourceDefinitionSnapshot] = []
        for resource in resources:
            if not (resource.is_skill_manifest and resource.uri.startswith("skill://")
                    and resource.uri.endswith("/_manifest") and not _is_unsupported_mime(resource.mime_type)):
                continue
            client = self._get_mcp_client(resource.source_id)
            async with client:
                manifest = await get_skill_manifest(client, resource.uri[len("skill://"):-len("/_manifest")])
            expanded.extend(ResourceDefinitionSnapshot(
                source_id=resource.source_id,
                uri=_resolve_relative_resource_uri(quote(file.path, safe="/"), resource.uri), name=file.path,
            ) for file in manifest.files)
        return expanded

    def _authorize_resource(self, source_id: str, uri: str, context: ResourceToolContext) -> str:
        resolved_uri = self._resolve_local_resource_path(uri).as_uri() if source_id == "local" else uri
        if not context.capability_catalog.resource_is_available(source_id, resolved_uri):
            raise ToolException(f"Resource not found: {uri}")
        return resolved_uri

    async def _read_resource_contents(self, source_id: str, uri: str) -> list[TextResourceContents | BlobResourceContents]:
        if source_id == "local":
            path = self._resolve_local_resource_path(uri)
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise ToolException(f"Cannot read resource {uri}: {exc}") from exc
            mime_type = mimetypes.guess_type(path.name)[0]
            if mime_type and mime_type.startswith("image/"):
                return [BlobResourceContents(uri=uri, mime_type=mime_type, blob=base64.b64encode(raw).decode("ascii"))]
            if mime_type is None or _is_text_mime(mime_type):
                try:
                    return [TextResourceContents(uri=uri, mime_type=mime_type, text=raw.decode("utf-8"))]
                except UnicodeDecodeError:
                    pass
            return [BlobResourceContents(uri=uri, mime_type=mime_type, blob=base64.b64encode(raw).decode("ascii"))]
        client = self._get_mcp_client(source_id)
        try:
            address = AnyUrl(uri)
        except ValueError as exc:
            raise ToolException(f"Invalid resource URI: {uri}") from exc
        try:
            async with client:
                contents = await client.read_resource(address)
        except MCPError as exc:
            if exc.code in {CONNECTION_CLOSED, REQUEST_TIMEOUT}:
                raise
            raise ToolException(f"Cannot read resource {uri}: {exc}") from exc
        if any(_is_unsupported_mime(content.mime_type) for content in contents):
            return contents
        normalized: list[TextResourceContents | BlobResourceContents] = []
        for content in contents:
            if isinstance(content, BlobResourceContents) and _is_text_mime(content.mime_type):
                try:
                    text = base64.b64decode(content.blob, validate=True).decode("utf-8")
                except ValueError as exc:
                    raise ToolException(f"Resource cannot be decoded as UTF-8 text: {content.uri}") from exc
                normalized.append(TextResourceContents(uri=content.uri, mime_type=content.mime_type, text=text))
            else:
                normalized.append(content)
        return normalized

    async def read_resource(
        self, source_id: str, uri: str, base_uri: str | None = None, *, runtime: ToolRuntime[ResourceToolContext],
    ) -> list[ResourceContent]:
        """Read a complete URI, or resolve a relative reference against an explicit base_uri.

        Copy the current document URI as base_uri; directory bases must end in '/'.
        The selected source is unchanged. Returns text/images and actual content URIs.
        """
        resolved_uri = self._authorize_resource(source_id, _resolve_relative_resource_uri(uri, base_uri), runtime.context)
        if source_id == "local":
            known_mime = mimetypes.guess_type(self._resolve_local_resource_path(resolved_uri).name)[0]
        else:
            known_mime = next((entry.mime_type for entry in runtime.context.capability_catalog.resource_catalog.resources
                               if entry.source_id == source_id and entry.uri == resolved_uri), None)
        if _is_unsupported_mime(known_mime):
            return [create_text_block(text=f"Unsupported binary resource type: {known_mime} ({resolved_uri}). Only text and images can be read.")]
        contents = await self._read_resource_contents(source_id, resolved_uri)
        for content in contents:
            if _is_unsupported_mime(content.mime_type):
                return [create_text_block(text=f"Unsupported binary resource type: {content.mime_type} ({content.uri}). Only text and images can be read.")]
        blocks: list[ResourceContent] = []
        for content in contents:
            blocks.append(create_text_block(text=json.dumps({
                "source_id": source_id, "uri": content.uri, "mime_type": content.mime_type,
            }, ensure_ascii=False)))
            if isinstance(content, TextResourceContents):
                blocks.append(create_text_block(text=content.text))
            elif _normalize_mime_type(content.mime_type).startswith("image/"):
                blocks.append(create_image_block(base64=content.blob, mime_type=_normalize_mime_type(content.mime_type)))
            else:
                blocks.append(create_text_block(text=f"Unsupported binary resource type: {content.mime_type or 'unknown'}. Only text and images can be read."))
        return blocks

    async def load_skill(self, source_id: str, uri: str, *, runtime: ToolRuntime[ResourceToolContext]) -> list[ResourceContent]:
        """Load one SKILL.md into this task's retained instructions; never load attachments.

        Accepts nested Skill entries even if absent from the initial summaries.
        References remain unchanged; use read_resource with this URI as base_uri.
        """
        if not _is_skill_uri(source_id, uri):
            raise ToolException("load_skill requires a SKILL.md entry: local file URI or MCP skill URI.")
        return await self.read_resource(source_id, uri, runtime=runtime)

    async def list_resources(
        self, source_id: str | None = None, skill_uri: str | None = None, *, runtime: ToolRuntime[ResourceToolContext],
    ) -> str:
        """List resource metadata/templates, optionally for one source or recursive Skill package.

        A package includes nested files and nested SKILL.md as ordinary entries.
        Requires source_id when skill_uri is given. Listings are not read whitelists:
        templates may permit unlisted URIs; this does not enumerate a server's filesystem.
        """
        if skill_uri and (not source_id or not _is_skill_uri(source_id, skill_uri)):
            raise ToolException("skill_uri requires source_id and a valid SKILL.md entry URI.")
        if skill_uri and source_id:
            skill_uri = self._authorize_resource(source_id, skill_uri, runtime.context)
        catalog = runtime.context.capability_catalog.visible_resources()
        filtered = [entry for entry in catalog.resources
                    if (not source_id or entry.source_id == source_id)
                    and (not skill_uri or _is_resource_in_skill_package(skill_uri, entry.uri))]
        templates = [entry for entry in catalog.templates if (not source_id or entry.source_id == source_id)
                     and (not skill_uri or self._template_overlaps_skill_package(skill_uri, entry.uri_template))]
        return ResourceCatalogSnapshot(resources=filtered, templates=templates).model_dump_json(exclude={"local_root"})

    @staticmethod
    def _template_overlaps_skill_package(skill_uri: str, template: str) -> bool:
        prefix = template.split("{", 1)[0]
        address = urlsplit(skill_uri)
        package_prefix = urlunsplit((address.scheme, address.netloc, address.path.rsplit("/", 1)[0] + "/", "", ""))
        return package_prefix.startswith(prefix) or prefix.startswith(package_prefix)

    def build_resource_tools(self) -> tuple[BaseTool, ...]:
        return tuple(StructuredTool.from_function(coroutine=handler, handle_tool_error=True,
                                                  metadata={"tool_source": "builtin:skill"})
                     for handler in (self.load_skill, self.read_resource, self.list_resources))
