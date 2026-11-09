"""Run capability selection protects column boundaries and preview consistency."""
from pathlib import Path
import json
import os

import pytest

from app.domain.resources import ResourceCatalogSnapshot, ResourceDefinitionSnapshot, ToolCatalogSnapshot


def local_catalog(root: Path) -> ResourceCatalogSnapshot:
    return ResourceCatalogSnapshot(local_root=str(root), resources=[
        ResourceDefinitionSnapshot(source_id="local", uri=(root / path).as_uri(), name=path)
        for path in ("a/SKILL.md", "a/child/SKILL.md", "b/SKILL.md", "b/rules.md", "shared.md")
    ])


def test_other_local_resources_exclude_unselected_packages_and_allow_unlisted_siblings(tmp_path: Path) -> None:
    from app.domain.resources import build_capability_preview, select_capabilities

    preview = build_capability_preview(ToolCatalogSnapshot(tools=[]), local_catalog(tmp_path), [])
    selected = [column for column in preview.columns if column.id == "local:other"]
    snapshot = select_capabilities(preview, selected)
    assert {column.id: column.selected for column in snapshot.columns} == {
        (tmp_path / "a/SKILL.md").as_uri(): False,
        (tmp_path / "b/SKILL.md").as_uri(): False,
        "local:other": True,
    }
    assert snapshot.resource_is_available("local", (tmp_path / "shared.md").as_uri())
    assert snapshot.resource_is_available("local", (tmp_path / "unlisted.md").as_uri())
    assert not snapshot.resource_is_available("local", (tmp_path / "b/unlisted.md").as_uri())
    assert not snapshot.resource_is_available("local", (tmp_path / "a/child/SKILL.md").as_uri())


def test_fingerprint_ignores_directory_order_but_rejects_selected_definition_changes(tmp_path: Path) -> None:
    from app.domain.resources import CapabilityCatalogChanged, build_capability_preview, select_capabilities

    resources = local_catalog(tmp_path)
    preview = build_capability_preview(ToolCatalogSnapshot(tools=[]), resources, ["mcp:empty"])
    reordered = resources.model_copy(update={"resources": list(reversed(resources.resources))})
    refreshed = build_capability_preview(ToolCatalogSnapshot(tools=[]), reordered, ["mcp:new", "mcp:empty"])
    assert {column.id: column.fingerprint for column in preview.columns} == {
        column.id: column.fingerprint for column in refreshed.columns if column.id != "mcp:new"
    }
    snapshot = select_capabilities(refreshed, preview.columns)
    assert next(column for column in snapshot.columns if column.id == "mcp:new").selected is False
    changed = resources.model_copy(update={"resources": [
        entry.model_copy(update={"description": "changed"}) if entry.name == "b/rules.md" else entry
        for entry in resources.resources
    ]})
    changed_preview = build_capability_preview(ToolCatalogSnapshot(tools=[]), changed, ["mcp:empty"])
    with pytest.raises(CapabilityCatalogChanged):
        select_capabilities(changed_preview, preview.columns)
    other_only = [column for column in preview.columns if column.id == "local:other"]
    assert select_capabilities(changed_preview, other_only).resource_is_available("local", (tmp_path / "shared.md").as_uri())


def test_mcp_column_allows_unlisted_uris_and_empty_selection_denies_resources(tmp_path: Path) -> None:
    from app.domain.resources import build_capability_preview, select_capabilities

    preview = build_capability_preview(ToolCatalogSnapshot(tools=[]), local_catalog(tmp_path), ["mcp:first", "mcp:second"])
    selected = select_capabilities(preview, [column for column in preview.columns if column.id == "mcp:first"])
    assert selected.resource_is_available("mcp:first", "doc://unlisted")
    assert not selected.resource_is_available("mcp:second", "doc://unlisted")
    empty = select_capabilities(preview, [])
    assert not empty.resource_is_available("mcp:first", "doc://unlisted")
    assert not empty.resource_is_available("local", (tmp_path / "shared.md").as_uri())


@pytest.mark.parametrize("change", ["resource_added", "resource_removed", "column_removed", "tool_schema", "template"])
def test_selected_column_directory_changes_require_a_new_preview(tmp_path: Path, change: str) -> None:
    from app.domain.resources import (
        CapabilityCatalogChanged, ResourceTemplateSnapshot, ToolAnnotationsSnapshot, ToolDefinitionSnapshot,
        build_capability_preview, select_capabilities,
    )

    tool = ToolDefinitionSnapshot(name="fixture_inspect", source_id="mcp:fixture", description="Inspect",
                                  input_schema={"type": "object"}, annotations=ToolAnnotationsSnapshot())
    resource = ResourceDefinitionSnapshot(source_id="mcp:fixture", uri="doc://a", name="A")
    template = ResourceTemplateSnapshot(source_id="mcp:fixture", uri_template="doc://{name}", name="Docs")
    resources = ResourceCatalogSnapshot(local_root=str(tmp_path), resources=[resource], templates=[template])
    preview = build_capability_preview(ToolCatalogSnapshot(tools=[tool]), resources, ["mcp:fixture"])
    selected = [column for column in preview.columns if column.id == "mcp:fixture"]
    refreshed_tools = [tool]
    refreshed_sources = ["mcp:fixture"]
    if change == "resource_added":
        resources = resources.model_copy(update={"resources": [resource, resource.model_copy(update={"uri": "doc://b", "name": "B"})]})
    elif change == "resource_removed":
        resources = resources.model_copy(update={"resources": []})
    elif change == "column_removed":
        resources = resources.model_copy(update={"resources": [], "templates": []})
        refreshed_tools, refreshed_sources = [], []
    elif change == "tool_schema":
        refreshed_tools = [tool.model_copy(update={"input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}})]
    else:
        resources = resources.model_copy(update={"templates": [template.model_copy(update={"uri_template": "doc://group/{name}"})]})
    refreshed = build_capability_preview(ToolCatalogSnapshot(tools=refreshed_tools), resources, refreshed_sources)
    with pytest.raises(CapabilityCatalogChanged):
        select_capabilities(refreshed, selected)


def test_startup_preview_and_empty_selection_keep_only_builtin_tools(tmp_path: Path) -> None:
    from mcp_support import build_client, create_plan, finish_turn, tool_turn, wait_for_run, read_records

    with build_client(tmp_path, turns=[tool_turn(1), finish_turn(2)]) as client:
        preview_response = client.get("/api/capabilities")
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert {column["id"] for column in preview["columns"]} == {"mcp:fixture", "local:other"}
        plan = create_plan(client)
        body = {"test_case_id": plan["test_case_id"], "test_plan_id": plan["id"], "assumptions_confirmed": True}
        assert client.post("/api/runs", json=body).status_code == 422
        duplicate = client.post("/api/runs", json={**body, "selected_columns": [preview["columns"][0]] * 2})
        assert duplicate.status_code == 422
        assert client.get("/api/runs").json()["total"] == 0
        response = client.post("/api/runs", json={**body, "selected_columns": []})
        assert response.status_code == 201, response.text
        detail = wait_for_run(client, response.json()["id"])
        assert detail["run"]["verdict"] == "PASS"
        catalog = detail["environment"]["capability_catalog"]
        assert not any(column["selected"] for column in catalog["columns"])
        assert {tool["name"] for tool in catalog["tool_catalog"]["tools"]} >= {
            "fixture_inspect", "finish_task", "read_resource", "load_skill", "list_resources", "read_image_artifact",
        }
        assert detail["task_runs"][0]["cycle_count"] == 2
        assert not any(record["phase"] == "started" for record in read_records(tmp_path))


def test_changed_selected_column_rejects_preparation_and_refresh_recovers(tmp_path: Path) -> None:
    from mcp_support import build_client, create_plan, finish_turn

    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        selection = client.get("/api/capabilities").json()["columns"]
        plan = create_plan(client)
        scenario_path = tmp_path / "scenario.json"
        scenario = json.loads(scenario_path.read_text("utf-8"))
        scenario["tools"][0]["description"] = "New tool definition"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        body = {"test_case_id": plan["test_case_id"], "test_plan_id": plan["id"],
                "assumptions_confirmed": True, "selected_columns": selection}
        rejected = client.post("/api/runs", json=body)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "capability_catalog_changed"
        assert client.get("/api/runs").json()["total"] == 0
        refreshed = client.post("/api/capabilities/refresh")
        assert refreshed.status_code == 200
        response = client.post("/api/runs", json={**body, "selected_columns": refreshed.json()["columns"]})
        assert response.status_code == 201, response.text


def test_startup_discovery_failure_keeps_http_available_and_can_be_retried(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from app.main import create_app
    from config_support import build_settings

    with TestClient(create_app(build_settings(tmp_path))) as client:
        assert client.get("/api/test-cases").status_code == 200
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/capabilities").status_code == 502
        (tmp_path / "mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
        refreshed = client.post("/api/capabilities/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert [column["id"] for column in refreshed.json()["columns"]] == ["local:other"]


@pytest.mark.asyncio
async def test_local_other_scope_blocks_package_aliases_before_reading_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_core.tools import ToolException
    from app.resources.static_resources import SkillResourceProvider
    from app.resources.mcp import MCPClientProvider
    from mcp_support import resource_runtime

    (tmp_path / "b").mkdir()
    (tmp_path / "b/SKILL.md").write_text("---\nname: B\ndescription: B package\n---", encoding="utf-8")
    secret = tmp_path / "b/secret.md"
    secret.write_text("unselected secret", encoding="utf-8")
    (tmp_path / "a.md").write_text("Read [B](b/secret.md)", encoding="utf-8")
    provider = SkillResourceProvider(tmp_path, MCPClientProvider(tmp_path / "unused.json"))
    await provider.discover_resources()
    runtime = resource_runtime(provider, {"local:other"})
    reads: list[Path] = []
    read_bytes = Path.read_bytes

    def record_read(path: Path) -> bytes:
        reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", record_read)
    aliases = ["b/secret.md", "b/../b/secret.md", "%62/secret.md"]
    if os.name == "nt":
        aliases.append("B/secret.md")
    for uri in aliases:
        with pytest.raises(ToolException, match="Resource not found"):
            await provider.read_resource("local", uri, base_uri=(tmp_path / "a.md").as_uri(), runtime=runtime)
    assert reads == []
    (tmp_path / "new.md").write_text("new same-column resource", encoding="utf-8")
    loaded = await provider.read_resource("local", "new.md", base_uri=(tmp_path / "a.md").as_uri(), runtime=runtime)
    assert any(block.get("text") == "new same-column resource" for block in loaded)
    listed = json.loads(await provider.list_resources(runtime=runtime))
    assert {resource["name"] for resource in listed["resources"]} == {"a.md"}


@pytest.mark.asyncio
async def test_unselected_mcp_is_neither_listed_nor_contacted_by_resource_tools(tmp_path: Path) -> None:
    from app.resources.static_resources import SkillResourceProvider
    from mcp_support import load_mcp_client_provider, write_mcp_config, invoke_resource_tool, read_records

    uri = "doc://unlisted"
    config = write_mcp_config(tmp_path, [], tools_enabled=False,
                             resource_contents={uri: [{"uri": uri, "text": "same-source hidden content"}]})
    provider = SkillResourceProvider(tmp_path / "skills", await load_mcp_client_provider(config))
    await provider.discover_resources()
    before = read_records(tmp_path)
    denied = await invoke_resource_tool(provider, "read_resource", {"source_id": "mcp:fixture", "uri": uri}, set())
    assert denied.status == "error" and "Resource not found" in denied.text
    listing = await invoke_resource_tool(provider, "list_resources", {}, set())
    assert json.loads(listing.text) == {"resources": [], "templates": []}
    assert read_records(tmp_path) == before
    allowed = await invoke_resource_tool(provider, "read_resource", {"source_id": "mcp:fixture", "uri": uri}, {"mcp:fixture"})
    assert allowed.status == "success" and "same-source hidden content" in allowed.text


@pytest.mark.asyncio
async def test_refresh_does_not_replace_clients_bound_to_an_existing_batch(tmp_path: Path) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.prebuilt import ToolNode
    from app.domain.resources import select_capabilities
    from app.resources.catalog import CapabilityProvider, CapabilityDiscovery
    from mcp_support import write_mcp_config, ResourceTestContext

    uri = "doc://manual"
    original = write_mcp_config(tmp_path / "original", [], resource_contents={uri: [{"uri": uri, "text": "original source"}]})
    replacement = write_mcp_config(tmp_path / "replacement", [], resource_contents={uri: [{"uri": uri, "text": "replacement source"}]})
    provider = CapabilityProvider(original, tmp_path / "skills", ())
    first = await provider.discover(30)
    original.write_text(replacement.read_text("utf-8"), encoding="utf-8")
    second = await provider.discover(30)

    async def read_from(discovery: CapabilityDiscovery) -> str:
        graph = StateGraph(MessagesState, context_schema=ResourceTestContext)
        graph.add_node("tools", ToolNode(discovery.tools))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        context = ResourceTestContext(select_capabilities(discovery.catalog, discovery.catalog.columns))
        result = await graph.compile().ainvoke({"messages": [AIMessage(content="", tool_calls=[{
            "name": "read_resource", "args": {"source_id": "mcp:fixture", "uri": uri}, "id": "read",
        }])]}, context=context)
        return result["messages"][-1].text

    assert "original source" in await read_from(first)
    assert "replacement source" in await read_from(second)
