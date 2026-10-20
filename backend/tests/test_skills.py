"""Progressive disclosure, package boundaries and source-bound resource reads."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from langchain_core.tools import ToolException


def write_nested_skills(root: Path) -> Path:
    parent = root / "base" / "parent"
    child = parent / "child"
    (child / "assets").mkdir(parents=True)
    (parent / "SKILL.md").write_text(
        "---\nname: Parent\ndescription: Parent instructions\n---\n"
        "Read [child](child/SKILL.md) only when needed.", encoding="utf-8",
    )
    (child / "SKILL.md").write_text(
        "---\nname: Child\ndescription: Child instructions\n---\n"
        "Look at ![example](assets/example.png).", encoding="utf-8",
    )
    (child / "assets" / "example.png").write_bytes(b"example image")
    (parent / "guide.md").write_text("Shared guide", encoding="utf-8")
    other = root / "external" / "parent-other"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: Other\ndescription: Other instructions\n---\nOther", encoding="utf-8")
    return parent


@pytest.mark.asyncio
async def test_local_nested_skills_disclose_only_roots_and_list_child_files(tmp_path: Path) -> None:
    from app.skill import SkillResourceProvider, top_level_skills

    root = tmp_path / "skills"
    parent = write_nested_skills(root)
    provider = SkillResourceProvider(root, {})
    catalog = await provider.discover()
    assert {entry.name for entry in top_level_skills(catalog)} == {"Parent", "Other"}
    parent_listing = await provider.list_resources("local", (parent / "SKILL.md").as_uri())
    assert {entry.uri for entry in parent_listing.resources} == {
        (parent / name).as_uri() for name in (
            "SKILL.md", "guide.md", "child/SKILL.md", "child/assets/example.png",
        )
    }
    child_listing = await provider.list_resources("local", (parent / "child" / "SKILL.md").as_uri())
    assert {entry.uri for entry in child_listing.resources} == {
        (parent / "child" / name).as_uri() for name in ("SKILL.md", "assets/example.png")
    }
    loaded = await provider.load_skill("local", (parent / "child" / "SKILL.md").as_uri())
    text = "\n".join(block["text"] for block in loaded if block["type"] == "text")
    assert "![example](assets/example.png)" in text
    assert not any(block["type"] == "image" for block in loaded)
    assert "Child instructions" not in catalog.model_dump_json()


@pytest.mark.asyncio
async def test_relative_reads_are_explicit_dynamic_and_confined(tmp_path: Path) -> None:
    from app.skill import SkillResourceProvider

    root = tmp_path / "skills"
    parent = write_nested_skills(root)
    provider = SkillResourceProvider(root, {})
    await provider.discover()
    base = (parent / "child" / "SKILL.md").as_uri()
    image = await provider.read_resource("local", "assets/example.png", base)
    assert any(block["type"] == "image" and block.get("mime_type") == "image/png" for block in image)
    (parent / "guide.md").write_text("Updated guide", encoding="utf-8")
    for reference, base_url in (("../guide.md", base), ("guide.md", parent.as_uri() + "/"),
                                ((parent / "guide.md").as_uri(), "invalid base")):
        blocks = await provider.read_resource("local", reference, base_url)
        assert any(block.get("text") == "Updated guide" for block in blocks)
    with pytest.raises(ToolException, match="base_url"):
        await provider.read_resource("local", "guide.md")
    with pytest.raises(ToolException):
        await provider.read_resource("local", (tmp_path / "outside.md").as_uri())
    with pytest.raises(ToolException):
        await provider.read_resource("local", "missing.png", base)
    (parent / "中文 文档.md").write_text("Encoded reference", encoding="utf-8")
    blocks = await provider.read_resource("local", "%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md", parent.as_uri() + "/")
    assert any(block.get("text") == "Encoded reference" for block in blocks)


@pytest.mark.asyncio
async def test_resource_tools_return_model_visible_read_errors(tmp_path: Path) -> None:
    from app.skill import SkillResourceProvider

    provider = SkillResourceProvider(tmp_path, {})
    await provider.discover()
    tools = {entry.name: entry for entry in provider.tools()}
    result = await tools["read_resource"].ainvoke({
        "name": "read_resource", "args": {"source_id": "local", "uri": "missing.md"},
        "id": "missing", "type": "tool_call",
    })
    assert result.status == "error" and "base_url" in result.text
    result = await tools["list_resources"].ainvoke({
        "name": "list_resources", "args": {}, "id": "listing", "type": "tool_call",
    })
    assert json.loads(result.content_blocks[0]["text"])["resources"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read_resource", "load_skill"])
@pytest.mark.parametrize("source_id", ["local", "mcp:fixture"])
async def test_malformed_resource_addresses_are_model_visible_errors(tmp_path: Path, tool_name: str, source_id: str) -> None:
    from app.mcp import load_mcp_clients
    from app.skill import SkillResourceProvider
    from mcp_support import write_mcp_config

    clients = await load_mcp_clients(write_mcp_config(tmp_path, [], tools_enabled=False)) if source_id != "local" else {}
    provider = SkillResourceProvider(tmp_path, clients)
    await provider.discover()
    tools = {entry.name: entry for entry in provider.tools()}
    result = await tools[tool_name].ainvoke({
        "name": tool_name, "args": {"source_id": source_id, "uri": f"{'file' if source_id == 'local' else 'skill'}://[broken/SKILL.md"},
        "id": "malformed", "type": "tool_call",
    })
    assert result.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["invalid%00.md", "guide:part.md"])
async def test_invalid_local_file_addresses_are_model_visible_errors(tmp_path: Path, filename: str) -> None:
    from app.skill import SkillResourceProvider

    provider = SkillResourceProvider(tmp_path, {})
    result = await provider.tools()[1].ainvoke({
        "name": "read_resource", "args": {"source_id": "local", "uri": tmp_path.as_uri() + "/" + filename},
        "id": "invalid-path", "type": "tool_call",
    })
    assert result.status == "error"


@pytest.mark.asyncio
async def test_mcp_manifest_nested_resources_and_source_bound_reads(tmp_path: Path) -> None:
    from app.mcp import load_mcp_clients
    from app.skill import SkillResourceProvider, top_level_skills
    from app.tools import MCPToolProvider
    from mcp_support import PNG_BASE64, read_records, write_mcp_config

    meta = {"fastmcp": {"skill": {"name": "Parent", "is_manifest": False}}}
    resources = [
        {"uri": "skill://parent/SKILL.md", "name": "parent/SKILL.md", "description": "Wrapped summary", "mimeType": "text/markdown", "_meta": meta},
        {"uri": "skill://parent/_manifest", "name": "manifest", "mimeType": "application/json", "_meta": {"fastmcp": {"skill": {"name": "parent", "is_manifest": True}}}},
        {"uri": "skill://parent-other/SKILL.md", "name": "Other", "description": "Other summary"},
        {"uri": "doc://manual", "name": "Manual", "_meta": {"fastmcp": "unrelated metadata"}},
    ]
    paths = ["SKILL.md", "child/SKILL.md", "child/assets/example.png", "guide.md"]
    contents = {
        "skill://parent/SKILL.md": [{"uri": "skill://parent/SKILL.md", "text": "---\nname: Wrong\ndescription: Wrong\n---\nParent body"}],
        "skill://parent/_manifest": [{"uri": "skill://parent/_manifest", "text": json.dumps({"skill": "parent", "files": [{"path": path, "size": 1, "hash": "sha256:test"} for path in paths]})}],
        "skill://parent/child/SKILL.md": [{"uri": "skill://parent/child/SKILL.md", "text": "![example](assets/example.png)"}],
        "skill://parent/child/assets/example.png": [{"uri": "skill://parent/child/assets/example.png", "mimeType": "image/png", "blob": PNG_BASE64}],
        "doc://manual": [{"uri": "doc://manual", "text": "Manual text"}],
        "doc://binary": [{"uri": "doc://binary", "mimeType": "application/pdf", "blob": "AA=="}],
    }
    config = write_mcp_config(tmp_path, [], resources=resources, resource_contents=contents,
                             templates=[{"uriTemplate": "skill://parent/{path*}", "name": "attachments"}], tools_enabled=False)
    assert await MCPToolProvider(config).discover_tools() == ()
    provider = SkillResourceProvider(tmp_path / "skills", await load_mcp_clients(config))
    catalog = await provider.discover()
    assert {entry.name for entry in top_level_skills(catalog)} == {"Parent", "Other"}
    assert not any(record["phase"] == "resource.read" for record in read_records(tmp_path))
    parent_listing = await provider.list_resources("mcp:fixture", "skill://parent/SKILL.md")
    assert {entry.uri for entry in parent_listing.resources} == {
        "skill://parent/SKILL.md", "skill://parent/_manifest", "skill://parent/guide.md",
        "skill://parent/child/SKILL.md", "skill://parent/child/assets/example.png",
    }
    child = await provider.list_resources("mcp:fixture", "skill://parent/child/SKILL.md")
    assert {entry.uri for entry in child.resources} == {
        "skill://parent/child/SKILL.md", "skill://parent/child/assets/example.png",
    }
    assert len(child.templates) == 1
    loaded = await provider.load_skill("mcp:fixture", "skill://parent/child/SKILL.md")
    assert any(block.get("text") == "![example](assets/example.png)" for block in loaded)
    images = await provider.read_resource("mcp:fixture", "assets/example.png", "skill://parent/child/SKILL.md")
    assert any(block["type"] == "image" and block.get("base64") == PNG_BASE64 for block in images)
    assert any(block.get("text") == "Manual text" for block in await provider.read_resource("mcp:fixture", "doc://manual"))
    assert "Unsupported binary" in str(await provider.read_resource("mcp:fixture", "doc://binary"))
    with pytest.raises(ToolException):
        await provider.read_resource("mcp:fixture", "skill://parent/missing.png")
    assert provider.catalog == catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [False, True])
async def test_resource_connection_failure_propagates_instead_of_becoming_a_read_error(tmp_path: Path, manifest: bool) -> None:
    from mcp import MCPError
    from app.mcp import load_mcp_clients
    from app.skill import SkillResourceProvider
    from mcp_support import write_mcp_config

    uri = "skill://parent/_manifest" if manifest else "doc://disconnect"
    resources = [{"uri": uri, "name": "manifest", "_meta": {"fastmcp": {"skill": {"is_manifest": True}}}}] if manifest else []
    config = write_mcp_config(tmp_path, [], tools_enabled=False, resources=resources, resource_disconnects=[uri])
    provider = SkillResourceProvider(tmp_path / "skills", await load_mcp_clients(config))
    await provider.discover()
    with pytest.raises(MCPError):
        if manifest:
            await provider.list_resources("mcp:fixture", "skill://parent/SKILL.md")
        else:
            await provider.read_resource("mcp:fixture", uri)


@pytest.mark.asyncio
async def test_mcp_package_templates_use_uri_path_and_preserve_source_boundaries(tmp_path: Path) -> None:
    from app.mcp import load_mcp_clients
    from app.skill import SkillResourceProvider
    from mcp_support import write_mcp_config

    skill_uri = "skill://parent/SKILL.md?revision=a/b"
    config = write_mcp_config(tmp_path, [], servers=("first", "second"), tools_enabled=False,
        resources=[{"uri": skill_uri, "name": "Parent", "description": "Summary"},
                   {"uri": "skill://parent-other/guide.md", "name": "Unrelated"}],
        templates=[{"uriTemplate": "skill://parent/assets/{path*}", "name": "attachments"},
                   {"uriTemplate": "skill://parent-other/{path*}", "name": "unrelated"}])
    provider = SkillResourceProvider(tmp_path / "skills", await load_mcp_clients(config))
    await provider.discover()
    listing = await provider.list_resources("mcp:first", skill_uri)
    assert [(entry.source_id, entry.uri) for entry in listing.resources] == [("mcp:first", skill_uri)]
    assert [(entry.source_id, entry.uri_template) for entry in listing.templates] == [
        ("mcp:first", "skill://parent/assets/{path*}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("blob_text", [False, True])
async def test_mcp_fallback_reads_only_top_level_and_templates_do_not_invent_files(tmp_path: Path, blob_text: bool) -> None:
    from app.mcp import load_mcp_clients
    from app.skill import SkillResourceProvider, top_level_skills
    from mcp_support import read_records, write_mcp_config

    document = "---\nname: Content name\ndescription: Recovered summary\n---\nRoot body"
    root_content = {"uri": "skill://parent/SKILL.md", "mimeType": "text/markdown",
                    **({"blob": base64.b64encode(document.encode()).decode()} if blob_text else {"text": document})}
    config = write_mcp_config(tmp_path, [], resources=[
        {"uri": "skill://parent/SKILL.md", "name": "Wrapped name"},
        {"uri": "skill://parent/child/SKILL.md", "name": "Child"},
    ], templates=[{"uriTemplate": "skill://parent/{path*}", "name": "files"}],
        resource_contents={
            "skill://parent/SKILL.md": [root_content],
            "skill://parent/hidden.md": [{"uri": "skill://parent/hidden.md", "text": "Hidden reference"}],
        }, tools_enabled=False)
    provider = SkillResourceProvider(tmp_path / "skills", await load_mcp_clients(config))
    catalog = await provider.discover()
    summaries = top_level_skills(catalog)
    assert [(entry.name, entry.description) for entry in summaries] == [("Wrapped name", "Recovered summary")]
    assert [entry["uri"] for entry in read_records(tmp_path) if entry["phase"] == "resource.read"] == ["skill://parent/SKILL.md"]
    listing = await provider.list_resources("mcp:fixture", "skill://parent/SKILL.md")
    assert {entry.uri for entry in listing.resources} == {"skill://parent/SKILL.md", "skill://parent/child/SKILL.md"}
    assert len(listing.templates) == 1
    assert any(block.get("text") == "Hidden reference" for block in await provider.read_resource("mcp:fixture", "skill://parent/hidden.md"))


def test_skill_ancestry_is_scoped_to_its_source() -> None:
    from app.domain.resources.static_resources import ResourceCatalogSnapshot, ResourceDefinitionSnapshot
    from app.skill import top_level_skills

    catalog = ResourceCatalogSnapshot(resources=[
        ResourceDefinitionSnapshot(source_id="mcp:first", uri="skill://parent/SKILL.md", name="Parent"),
        ResourceDefinitionSnapshot(source_id="mcp:second", uri="skill://parent/child/SKILL.md", name="Independent child"),
    ])
    assert [entry.name for entry in top_level_skills(catalog)] == ["Parent", "Independent child"]


def test_run_freezes_metadata_and_routes_skill_images_through_existing_evidence(tmp_path: Path) -> None:
    from langchain_core.messages import AIMessage
    from mcp_support import build_client, create_plan, finish_turn, replay_call, start_run, tool_turn, wait_for_run

    root = tmp_path / "skills"
    parent = write_nested_skills(root)
    child_uri = (parent / "child" / "SKILL.md").as_uri()
    # Use a real image in the same integration path as ordinary tool screenshots.
    from mcp_support import PNG_BASE64
    import base64
    (parent / "child" / "assets" / "example.png").write_bytes(base64.b64decode(PNG_BASE64))
    turns = [
        AIMessage(content="Load child instructions", tool_calls=[{"id": "load", "name": "load_skill", "args": {"source_id": "local", "uri": child_uri}}]),
        AIMessage(content="Read referenced example", tool_calls=[{"id": "read", "name": "read_resource", "args": {"source_id": "local", "uri": "assets/example.png", "base_url": child_uri}}]),
        tool_turn(3), finish_turn(4), tool_turn(5), finish_turn(6),
    ]
    with build_client(tmp_path, skills_dir=root, screenshot_history_rounds=1, calls=[replay_call(), replay_call()], turns=turns) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        catalog = detail["environment"]["resource_catalog"]
        assert catalog["local_root"] == str(root.resolve())
        assert "Child instructions" not in json.dumps(catalog)
        report = client.get(f"/api/runs/{run_id}").json()
        assert len([entry for entry in report["artifacts"] if entry["type"] == "screenshot"]) == 3
        messages = [entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"]
        initial = next(entry for entry in messages if entry["role"] == "human")
        initial_text = "\n".join(block["text"] for block in initial["content"] if block["type"] == "text")
        assert '"name": "Parent"' in initial_text and "Child instructions" not in initial_text
        assert any(entry["role"] == "tool" and entry["name"] == "load_skill" for entry in messages)
        second_task_messages = [entry["event"]["message"] for entry in report["events"]
                                if entry["event"]["type"] == "message.appended"
                                and entry["event"]["task_run_id"] == detail["task_runs"][1]["id"]]
        assert "![example](assets/example.png)" not in json.dumps(second_task_messages)
        loaded_message = next(entry for entry in messages if entry["role"] == "tool" and entry["name"] == "load_skill")
        assert all(block["type"] != "image_artifact" for block in loaded_message["content"])
        for export_format in ("json", "html"):
            exported = client.post(f"/api/runs/{run_id}/exports", json={"format": export_format})
            assert exported.status_code == 201
            download = client.get(f"/api/artifacts/{exported.json()['id']}")
            if export_format == "json":
                assert download.json()["detail"]["environment"]["resource_catalog"] == catalog
            else:
                assert "Skill 与静态资源" in download.text and "data:image/png;base64," in download.text
                assert (parent / "SKILL.md").as_uri() in download.text
