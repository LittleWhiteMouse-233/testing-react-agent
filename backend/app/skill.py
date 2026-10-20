"""Progressively disclose local Skills and source-bound MCP static resources."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import url2pathname

import frontmatter
from fastmcp import Client
from fastmcp.utilities.skills import get_skill_manifest
from langchain_core.messages.content import ImageContentBlock, TextContentBlock, create_image_block, create_text_block
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT, BlobResourceContents, TextResourceContents
from pydantic import AnyUrl
from rfc3986 import uri_reference
from rfc3986.exceptions import ResolutionError

from app.domain.resources.static_resources import (
    ResourceCatalogSnapshot, ResourceDefinitionSnapshot, ResourceTemplateSnapshot,
)


ResourceContent = TextContentBlock | ImageContentBlock
LOAD_SKILL_TOOL_NAME = "load_skill"
_TEXT_APPLICATION_MIMES = {"application/json", "application/xml", "application/yaml", "application/x-yaml", "application/toml", "application/javascript"}


def _is_text_mime(mime_type: str | None) -> bool:
    return bool(mime_type and (mime_type.startswith("text/") or mime_type in _TEXT_APPLICATION_MIMES))


def _is_skill_uri(source_id: str, uri: str) -> bool:
    try:
        address = urlsplit(uri)
    except ValueError:
        return False
    return address.path.rsplit("/", 1)[-1] == "SKILL.md" and (
        address.scheme == "file" if source_id == "local" else address.scheme == "skill"
    )


def _package_contains(skill_uri: str, resource_uri: str) -> bool:
    skill_address, resource_address = urlsplit(skill_uri), urlsplit(resource_uri)
    root_path = skill_address.path.rsplit("/", 1)[0] + "/"
    return (skill_address.scheme, skill_address.netloc) == (resource_address.scheme, resource_address.netloc) and resource_address.path.startswith(root_path)


def top_level_skills(catalog: ResourceCatalogSnapshot) -> list[ResourceDefinitionSnapshot]:
    """Derive roots from discovered addresses; no duplicated parent/child tree."""
    candidates = [entry for entry in catalog.resources if _is_skill_uri(entry.source_id, entry.uri)]
    return [entry for entry in candidates if not any(
        ancestor.source_id == entry.source_id
        and len(urlsplit(ancestor.uri).path) < len(urlsplit(entry.uri).path)
        and _package_contains(ancestor.uri, entry.uri)
        for ancestor in candidates
    )]


def _resolve_resource_uri(uri: str, base_url: str | None = None) -> str:
    reference = uri_reference(uri)
    if reference.scheme:
        return uri
    if not base_url or not uri_reference(base_url).scheme:
        raise ToolException("Relative resource uri requires an absolute base_url (copy the document URI).")
    try:
        return reference.resolve_with(base_url).unsplit()
    except (ResolutionError, ValueError) as exc:
        raise ToolException(f"Invalid resource URI or base_url: {exc}") from exc


class SkillResourceProvider:
    """A run's frozen catalog and clients; content is always read on demand."""

    def __init__(self, local_root: Path, clients: dict[str, Client[Any]]) -> None:
        self.local_root = local_root.resolve()
        self.clients = {f"mcp:{name}": client for name, client in clients.items()}
        self.catalog = ResourceCatalogSnapshot(local_root=str(self.local_root))

    def _local_resources(self, directory: Path) -> list[ResourceDefinitionSnapshot]:
        return [ResourceDefinitionSnapshot(
            source_id="local", uri=path.as_uri(), name=path.relative_to(self.local_root).as_posix(),
            mime_type=mimetypes.guess_type(path.name)[0],
        ) for path in sorted(directory.rglob("*"))
            if path.is_file() and path.resolve().is_relative_to(self.local_root)]

    def _local_path(self, uri: str) -> Path:
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

    def _client(self, source_id: str) -> Client[Any]:
        if source_id not in self.clients:
            raise ToolException(f"Unknown resource source: {source_id}")
        return self.clients[source_id]

    async def discover(self) -> ResourceCatalogSnapshot:
        resources = await asyncio.to_thread(self._local_resources, self.local_root)
        templates: list[ResourceTemplateSnapshot] = []
        for source_id, client in self.clients.items():
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
        catalog = ResourceCatalogSnapshot(local_root=str(self.local_root), resources=resources, templates=templates)
        summaries: dict[tuple[str, str], ResourceDefinitionSnapshot] = {}
        for entry in top_level_skills(catalog):
            if entry.source_id == "local" or not entry.name or not entry.description:
                contents = await self._read_contents(entry.source_id, entry.uri)
                document = frontmatter.loads("\n".join(content.text for content in contents if isinstance(content, TextResourceContents)))
                name = document.get("name")
                description = document.get("description")
                summaries[(entry.source_id, entry.uri)] = entry.model_copy(update={
                    "name": (name if isinstance(name, str) and name else self._local_path(entry.uri).parent.name)
                        if entry.source_id == "local" else entry.name or (name if isinstance(name, str) else "SKILL.md"),
                    "description": entry.description or (description if isinstance(description, str) else None),
                })
        self.catalog = catalog.model_copy(update={"resources": [
            summaries.get((entry.source_id, entry.uri), entry) for entry in resources
        ]})
        return self.catalog

    async def _read_contents(self, source_id: str, uri: str) -> list[TextResourceContents | BlobResourceContents]:
        if source_id == "local":
            path = self._local_path(uri)
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
        client = self._client(source_id)
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

    async def read_resource(self, source_id: str, uri: str, base_url: str | None = None) -> list[ResourceContent]:
        """Read a complete URI, or resolve a relative reference against an explicit base_url.

        Copy the current document URI as base_url; directory bases must end in '/'.
        The selected source is unchanged. Returns text/images and actual content URIs.
        """
        resolved_uri = _resolve_resource_uri(uri, base_url)
        blocks: list[ResourceContent] = []
        for content in await self._read_contents(source_id, resolved_uri):
            blocks.append(create_text_block(text=json.dumps({
                "source_id": source_id, "uri": content.uri, "mime_type": content.mime_type,
            }, ensure_ascii=False)))
            if isinstance(content, TextResourceContents):
                blocks.append(create_text_block(text=content.text))
            elif content.mime_type and content.mime_type.startswith("image/"):
                blocks.append(create_image_block(base64=content.blob, mime_type=content.mime_type))
            else:
                blocks.append(create_text_block(text=f"Unsupported binary resource type: {content.mime_type or 'unknown'}. Only text and images can be read."))
        return blocks

    async def load_skill(self, source_id: str, uri: str) -> list[ResourceContent]:
        """Load one SKILL.md into this task's retained instructions; never load attachments.

        Accepts nested Skill entries even if absent from the initial summaries.
        References remain unchanged; use read_resource with this URI as base_url.
        """
        if not _is_skill_uri(source_id, uri):
            raise ToolException("load_skill requires a SKILL.md entry: local file URI or MCP skill URI.")
        return await self.read_resource(source_id, uri)

    async def list_resources(self, source_id: str | None = None, skill_uri: str | None = None) -> ResourceCatalogSnapshot:
        """List metadata, optionally scoped to a source and a recursive Skill package."""
        if skill_uri and (not source_id or not _is_skill_uri(source_id, skill_uri)):
            raise ToolException("skill_uri requires source_id and a valid SKILL.md entry URI.")
        if source_id and source_id != "local":
            self._client(source_id)
        resources = [entry for entry in self.catalog.resources if entry.source_id != "local"]
        if source_id in {None, "local"}:
            directory = self._local_path(skill_uri).parent if skill_uri else self.local_root
            resources.extend(await asyncio.to_thread(self._local_resources, directory))
        for entry in self.catalog.resources:
            if not entry.is_skill_manifest or (source_id and source_id != entry.source_id):
                continue
            # The public FastMCP helper uses precisely this advertised URI form.
            if not entry.uri.startswith("skill://") or not entry.uri.endswith("/_manifest"):
                continue
            manifest_skill_uri = entry.uri.rsplit("/", 1)[0] + "/SKILL.md"
            if skill_uri and not (_package_contains(manifest_skill_uri, skill_uri) or _package_contains(skill_uri, entry.uri)):
                continue
            client = self._client(entry.source_id)
            try:
                async with client:
                    manifest = await get_skill_manifest(client, entry.uri[len("skill://"):-len("/_manifest")])
                resources.extend(ResourceDefinitionSnapshot(
                    source_id=entry.source_id,
                    uri=_resolve_resource_uri(quote(file.path, safe="/"), entry.uri), name=file.path,
                ) for file in manifest.files)
            except (MCPError, ValueError) as exc:
                if isinstance(exc, MCPError) and exc.code in {CONNECTION_CLOSED, REQUEST_TIMEOUT}:
                    raise
                raise ToolException(f"Cannot list manifest {entry.uri}: {exc}") from exc
        filtered = [entry for entry in resources if (not source_id or entry.source_id == source_id)
                    and (not skill_uri or _package_contains(skill_uri, entry.uri))]
        # Prefer advertised metadata to manifest entries which know only paths.
        unique: dict[tuple[str, str], ResourceDefinitionSnapshot] = {}
        for entry in filtered:
            unique.setdefault((entry.source_id, entry.uri), entry)
        templates = [entry for entry in self.catalog.templates if (not source_id or entry.source_id == source_id)
                     and (not skill_uri or self._template_related(skill_uri, entry.uri_template))]
        return ResourceCatalogSnapshot(local_root=self.catalog.local_root,
                                       resources=list(unique.values()), templates=templates)

    @staticmethod
    def _template_related(skill_uri: str, template: str) -> bool:
        prefix = template.split("{", 1)[0]
        address = urlsplit(skill_uri)
        package_prefix = urlunsplit((address.scheme, address.netloc, address.path.rsplit("/", 1)[0] + "/", "", ""))
        return package_prefix.startswith(prefix) or prefix.startswith(package_prefix)

    def tools(self) -> tuple[BaseTool, ...]:
        async def list_resources(source_id: str | None = None, skill_uri: str | None = None) -> list[ResourceContent]:
            """List resource metadata/templates, optionally for one source or Skill package.

            A package includes nested files and nested SKILL.md as ordinary entries.
            Requires source_id when skill_uri is given. Listings are not read whitelists.
            """
            catalog = await self.list_resources(source_id, skill_uri)
            return [create_text_block(text=catalog.model_dump_json(exclude={"local_root"})),
                    create_text_block(text="Only discoverable resources are listed. Templates may permit unlisted URIs; this does not enumerate the server's physical filesystem.")]

        return tuple(StructuredTool.from_function(coroutine=handler, handle_tool_error=True)
                     for handler in (self.load_skill, self.read_resource, list_resources))
