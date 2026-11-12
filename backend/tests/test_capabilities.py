"""Run capability selection protects column boundaries and preview consistency."""
from pathlib import Path
import json
import os

import pytest

from app.domain.resources import ResourceCatalogSnapshot, ResourceDefinitionSnapshot, ToolCatalogSnapshot
from app.resources.capabilities import CapabilityProvider


def build_capability_provider(root: Path, mcp_config: Path, *, timeout_seconds: float = 120) -> CapabilityProvider:
    from app.config import SettingsService
    from app.test_case_lock import TestCaseLock
    from config_support import build_settings

    return CapabilityProvider(mcp_config, root / "skills", (), settings_service=SettingsService(
        build_settings(root, mcp_config_path=mcp_config, capability_discovery_timeout_seconds=timeout_seconds), TestCaseLock(),
    ))


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
        assert catalog["columns"] == [{"id": "local:other", "selected": False}]
        assert {tool["name"] for tool in catalog["tool_catalog"]["tools"]} == {
            "finish_task", "read_resource", "load_skill", "list_resources", "read_image_artifact",
        }
        assert detail["task_runs"][0]["cycle_count"] == 2
        assert not any(record["phase"] == "started" for record in read_records(tmp_path))
        assert client.get("/api/capabilities").json() == preview

        scenario_path = tmp_path / "scenario.json"
        scenario = json.loads(scenario_path.read_text("utf-8"))
        scenario["tools"][0]["description"] = "Changed after the run"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        refreshed = client.post("/api/capabilities/refresh")
        assert refreshed.status_code == 200 and refreshed.json() != preview
        assert client.get(f"/api/runs/{response.json()['id']}").json()["detail"]["environment"]["capability_catalog"] == catalog
        for export_format in ("json", "html"):
            exported = client.post(f"/api/runs/{response.json()['id']}/exports", json={"format": export_format})
            assert exported.status_code == 201
            download = client.get(f"/api/artifacts/{exported.json()['id']}")
            assert download.status_code == 200
            if export_format == "json":
                assert download.json()["detail"]["environment"]["capability_catalog"] == catalog
            else:
                resource_section = download.text.split("<h3>工具目录</h3>", 1)[1].split("</section>", 1)[0]
                assert "finish_task" in resource_section and "local:other" in resource_section
                assert "mcp:fixture" not in resource_section


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
    catalog = await provider.discover_resources()
    runtime = resource_runtime(provider, {"local:other"}, resource_catalog=catalog)
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
    catalog = await provider.discover_resources()
    before = read_records(tmp_path)
    denied = await invoke_resource_tool(provider, "read_resource", {"source_id": "mcp:fixture", "uri": uri}, set(), resource_catalog=catalog)
    assert denied.status == "error" and "Resource not found" in denied.text
    listing = await invoke_resource_tool(provider, "list_resources", {}, set(), resource_catalog=catalog)
    assert json.loads(listing.text) == {"resources": [], "templates": []}
    assert read_records(tmp_path) == before
    allowed = await invoke_resource_tool(provider, "read_resource", {"source_id": "mcp:fixture", "uri": uri}, {"mcp:fixture"}, resource_catalog=catalog)
    assert allowed.status == "success" and "same-source hidden content" in allowed.text


@pytest.mark.asyncio
async def test_refresh_does_not_replace_clients_bound_to_an_existing_batch(tmp_path: Path) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.prebuilt import ToolNode
    from app.domain.resources import CapabilityColumnSelection, select_capabilities
    from app.resources.capabilities import CapabilityProvider, CapabilityDiscovery
    from mcp_support import write_mcp_config, ResourceTestContext

    uri = "doc://manual"
    original = write_mcp_config(tmp_path / "original", [], resource_contents={uri: [{"uri": uri, "text": "original source"}]})
    replacement = write_mcp_config(tmp_path / "replacement", [], resource_contents={uri: [{"uri": uri, "text": "replacement source"}]})
    from app.config import SettingsService
    from app.test_case_lock import TestCaseLock
    from config_support import build_settings

    provider = CapabilityProvider(original, tmp_path / "skills", (), settings_service=SettingsService(build_settings(tmp_path), TestCaseLock()))
    first = await provider.discover()
    original.write_text(replacement.read_text("utf-8"), encoding="utf-8")
    second = await provider.discover()

    async def read_from(discovery: CapabilityDiscovery[CapabilityColumnSelection]) -> str:
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


async def test_discovery_reads_updated_timeout_without_replacing_inflight_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from app.config import SettingsService, SettingsContent
    from app.domain.resources import LLMProfileSettings
    from app.resources.capabilities import CapabilityProvider
    from app.resources.mcp import MCPClientProvider, MCPConnectionError
    from app.test_case_lock import TestCaseLock
    from config_support import build_settings
    from mcp_support import write_mcp_config

    mcp_config = write_mcp_config(tmp_path, [])
    service = SettingsService(build_settings(tmp_path, capability_discovery_timeout_seconds=0.2), TestCaseLock())
    provider = CapabilityProvider(mcp_config, tmp_path / "skills", (), settings_service=service)
    entered, release = asyncio.Event(), asyncio.Event()
    original_load = MCPClientProvider.load_clients

    async def delayed_load(clients: MCPClientProvider) -> None:
        entered.set()
        await release.wait()
        await original_load(clients)

    monkeypatch.setattr(MCPClientProvider, "load_clients", delayed_load)
    first = asyncio.create_task(provider.discover())
    await asyncio.wait_for(entered.wait(), 2)
    updated = service.current.model_dump()
    updated["capability_discovery_timeout_seconds"] = 30
    await service.update(SettingsContent[LLMProfileSettings].model_validate(updated), version=service.version)
    with pytest.raises(MCPConnectionError):
        await first
    assert service.restart_required is False
    release.set()
    assert (await provider.discover()).catalog.columns


@pytest.mark.parametrize("selected_source_state", ["replaced", "empty", "removed"])
def test_targeted_preview_merge_replaces_whole_sources_and_local_boundaries(
    tmp_path: Path, selected_source_state: str,
) -> None:
    from app.domain.resources import (
        ResourceTemplateSnapshot, ToolAnnotationsSnapshot, ToolDefinitionSnapshot,
        build_capability_preview, merge_capability_preview, select_capabilities,
    )

    first_tool = ToolDefinitionSnapshot(name="first_old", source_id="mcp:first", description="Old",
                                       input_schema={"type": "object"}, annotations=ToolAnnotationsSnapshot())
    second_tool = first_tool.model_copy(update={"name": "second_inspect", "source_id": "mcp:second"})
    builtin_tool = first_tool.model_copy(update={"name": "finish", "source_id": "builtin:fixture"})
    first_resource = ResourceDefinitionSnapshot(source_id="mcp:first", uri="doc://old", name="Old")
    second_resource = first_resource.model_copy(update={"source_id": "mcp:second"})
    first_template = ResourceTemplateSnapshot(source_id="mcp:first", uri_template="doc://old/{path}", name="Old")
    second_template = first_template.model_copy(update={"source_id": "mcp:second"})
    cached = build_capability_preview(
        ToolCatalogSnapshot(tools=[first_tool, second_tool, builtin_tool]),
        ResourceCatalogSnapshot(local_root=str(tmp_path), resources=[
            *local_catalog(tmp_path).resources, first_resource, second_resource,
        ], templates=[first_template, second_template]),
        ["mcp:first", "mcp:second"],
    )
    cached_before_merge = cached.model_dump_json()
    fresh_local = ResourceDefinitionSnapshot(source_id="local", uri=(tmp_path / "new/SKILL.md").as_uri(), name="New")
    discovered = build_capability_preview(
        ToolCatalogSnapshot(tools=[builtin_tool.model_copy(update={"description": "Current"}), *(
            [first_tool.model_copy(update={"name": "first_new"})] if selected_source_state == "replaced" else []
        )]),
        ResourceCatalogSnapshot(local_root=str(tmp_path), resources=[fresh_local, *(
            [first_resource.model_copy(update={"uri": "doc://new"})] if selected_source_state == "replaced" else []
        )]),
        [] if selected_source_state == "removed" else ["mcp:first"],
    )

    merged = merge_capability_preview(cached, discovered, {"mcp:first"})

    assert {tool.name for tool in merged.tool_catalog.tools} == (
        {"finish", "second_inspect", "first_new"} if selected_source_state == "replaced"
        else {"finish", "second_inspect"}
    )
    assert next(tool for tool in merged.tool_catalog.tools if tool.name == "finish").description == "Current"
    expected_resources = {("local", fresh_local.uri), ("mcp:second", "doc://old")}
    if selected_source_state == "replaced":
        expected_resources.add(("mcp:first", "doc://new"))
    assert {(entry.source_id, entry.uri) for entry in merged.resource_catalog.resources} == expected_resources
    assert [(entry.source_id, entry.uri_template) for entry in merged.resource_catalog.templates] == [
        ("mcp:second", "doc://old/{path}"),
    ]
    assert ("mcp:first" in {column.id for column in merged.columns}) == (selected_source_state != "removed")
    assert next(column for column in merged.columns if column.id == "mcp:second").fingerprint == next(
        column for column in cached.columns if column.id == "mcp:second"
    ).fingerprint
    snapshot = select_capabilities(merged, [column for column in merged.columns if column.id == "local:other"])
    assert not snapshot.resource_is_available("local", (tmp_path / "new/unlisted.md").as_uri())
    assert cached.model_dump_json() == cached_before_merge


@pytest.mark.asyncio
async def test_discovery_reuses_one_process_for_tools_resources_manifests_and_summaries(tmp_path: Path) -> None:
    from mcp_support import read_records, write_mcp_config

    manifest_uri = "skill://package/_manifest"
    skill_uri = "skill://package/SKILL.md"
    config = write_mcp_config(tmp_path, [], resources=[
        {"uri": manifest_uri, "name": "manifest", "mimeType": "application/json",
         "_meta": {"fastmcp": {"skill": {"is_manifest": True}}}},
        {"uri": skill_uri, "name": "package", "mimeType": "text/markdown"},
    ], resource_contents={
        manifest_uri: [{"uri": manifest_uri, "text": json.dumps({
            "skill": "package", "files": [{"path": "SKILL.md", "size": 1, "hash": "sha256:test"}],
        })}],
        skill_uri: [{"uri": skill_uri, "text": "---\nname: Package\ndescription: Package summary\n---\nInstructions"}],
    })
    discovery = await build_capability_provider(tmp_path, config).discover()

    records = read_records(tmp_path)
    assert {record["uri"] for record in records if record["phase"] == "resource.read"} == {manifest_uri, skill_uri}
    assert len({record["pid"] for record in records}) == 1
    assert sum(record["phase"] == "closed" for record in records) == 1
    assert not any(record["phase"] == "started" for record in records)
    assert {tool.name for tool in discovery.tools} == {"load_skill", "read_resource", "list_resources"}


@pytest.mark.asyncio
async def test_targeted_discovery_keeps_cached_unselected_service_without_connecting_it(tmp_path: Path) -> None:
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [], servers=("first", "second"),
                              resources=[{"uri": "doc://manual", "name": "Manual"}],
                              templates=[{"uriTemplate": "doc://{name}", "name": "Documents"}])
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    initial_json = initial.catalog.model_dump_json()
    second_records = read_records(tmp_path, "second")
    connections = json.loads(config.read_text("utf-8"))
    connections["mcpServers"]["second"]["command"] = "nonexistent-mcp-executable"
    config.write_text(json.dumps(connections), encoding="utf-8")
    selection = [column for column in initial.catalog.columns if column.id == "mcp:first"]

    current = await provider.discover(selected_columns=selection)

    assert {column.id for column in current.catalog.columns} == {"mcp:first", "local:other"}
    assert {column.id: column.selected for column in current.catalog.columns} == {"mcp:first": True, "local:other": False}
    assert {tool.source_id for tool in current.catalog.tool_catalog.tools} == {"mcp:first", "builtin:skill"}
    assert {resource.source_id for resource in current.catalog.resource_catalog.resources} == {"mcp:first"}
    assert {template.source_id for template in current.catalog.resource_catalog.templates} == {"mcp:first"}
    assert not current.catalog.resource_is_available("mcp:second", "doc://unlisted")
    assert read_records(tmp_path, "second") == second_records
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "first")) == 2
    assert initial.catalog.model_dump_json() == initial_json
    assert provider.preview.columns == initial.catalog.columns
    assert {tool.source_id for tool in provider.preview.tool_catalog.tools} == {"mcp:first", "mcp:second", "builtin:skill"}
    assert {resource.source_id for resource in provider.preview.resource_catalog.resources} == {"mcp:first", "mcp:second"}
    assert {template.source_id for template in provider.preview.resource_catalog.templates} == {"mcp:first", "mcp:second"}


@pytest.mark.asyncio
@pytest.mark.parametrize("select_other", [False, True])
async def test_local_selection_refreshes_all_local_boundaries_without_connecting_mcp(tmp_path: Path, select_other: bool) -> None:
    from app.domain.resources import select_capabilities
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [])
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    before = read_records(tmp_path)
    new_package = tmp_path / "skills/new"
    new_package.mkdir(parents=True)
    (new_package / "SKILL.md").write_text("---\nname: New\ndescription: New package\n---", encoding="utf-8")

    selected_columns = [column for column in initial.catalog.columns if select_other and column.id == "local:other"]
    current = await provider.discover(selected_columns=selected_columns)

    assert read_records(tmp_path) == before
    assert {column.id for column in current.catalog.columns} == {(new_package / "SKILL.md").as_uri(), "local:other"}
    assert {column.id: column.selected for column in current.catalog.columns} == {
        (new_package / "SKILL.md").as_uri(): False, "local:other": select_other,
    }
    assert not current.catalog.resource_is_available("local", (new_package / "unlisted.md").as_uri())
    assert current.catalog.resource_is_available("local", (tmp_path / "skills/unlisted.md").as_uri()) is select_other
    assert {column.id for column in provider.preview.columns} == {"mcp:fixture", (new_package / "SKILL.md").as_uri(), "local:other"}
    old_snapshot = select_capabilities(initial.catalog, [column for column in initial.catalog.columns if column.id == "local:other"])
    assert old_snapshot.resource_is_available("local", (new_package / "unlisted.md").as_uri())


@pytest.mark.asyncio
async def test_catalog_conflict_publishes_new_preview_without_assembling_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.resources import CapabilityCatalogChanged
    from app.resources import tools as tool_resources
    from mcp_support import read_records, write_mcp_config

    async def reject_assembly(*arguments: object) -> None:
        raise AssertionError("A conflicting selection must not assemble executable tools")

    monkeypatch.setattr(tool_resources, "as_langchain_tool", reject_assembly)
    config = write_mcp_config(tmp_path, [])
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    initial_json = initial.catalog.model_dump_json()
    scenario_path = tmp_path / "scenario.json"
    scenario = json.loads(scenario_path.read_text("utf-8"))
    scenario["tools"][0]["description"] = "New definition"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    with pytest.raises(CapabilityCatalogChanged):
        await provider.discover(selected_columns=initial.catalog.columns, build_execution_tools=True)

    assert provider.preview.tool_catalog.tools[0].description == "New definition"
    assert provider.preview != initial.catalog
    assert initial.catalog.model_dump_json() == initial_json
    assert not any(record["phase"] == "started" for record in read_records(tmp_path))


@pytest.mark.asyncio
async def test_selected_service_removal_clears_its_cached_column_and_full_refresh_replaces_all(tmp_path: Path) -> None:
    from app.domain.resources import CapabilityCatalogChanged
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [], servers=("first", "second"))
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    before = {name: read_records(tmp_path, name) for name in ("first", "second")}
    connections = json.loads(config.read_text("utf-8"))
    del connections["mcpServers"]["first"]
    connections["mcpServers"]["second"]["command"] = "nonexistent-mcp-executable"
    config.write_text(json.dumps(connections), encoding="utf-8")

    with pytest.raises(CapabilityCatalogChanged):
        await provider.discover(selected_columns=[column for column in initial.catalog.columns if column.id == "mcp:first"])

    assert {column.id for column in provider.preview.columns} == {"mcp:second", "local:other"}
    assert all(tool.source_id != "mcp:first" for tool in provider.preview.tool_catalog.tools)
    assert {name: read_records(tmp_path, name) for name in before} == before
    write_mcp_config(tmp_path, [], servers=("third",))
    refreshed = await provider.discover()
    assert {column.id for column in refreshed.catalog.columns} == {"mcp:third", "local:other"}


@pytest.mark.asyncio
async def test_targeted_preparation_assembles_only_selected_service_and_keeps_calls_independent(tmp_path: Path) -> None:
    from langsmith import tracing_context
    from mcp_support import read_records, replay_call, text_block, write_mcp_config

    config = write_mcp_config(tmp_path, [replay_call(content=[text_block("observed")])], servers=("first", "second"))
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    second_records = read_records(tmp_path, "second")

    prepared = await provider.discover(
        selected_columns=[column for column in initial.catalog.columns if column.id == "mcp:first"],
        build_execution_tools=True,
    )

    assert {tool.name for tool in prepared.tools} == {"first_inspect", "load_skill", "read_resource", "list_resources"}
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "first")) == 2
    assert sum(record["phase"] == "discovery" for record in read_records(tmp_path, "first")) == 2
    tool = next(tool for tool in prepared.tools if tool.name == "first_inspect")
    with tracing_context(enabled=False):
        response = await tool.ainvoke({"name": tool.name, "args": {}, "id": "inspect", "type": "tool_call"})
    assert response.text == "observed"
    assert read_records(tmp_path, "second") == second_records
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "first")) == 3


@pytest.mark.asyncio
async def test_targeted_discovery_without_valid_preview_does_not_connect(tmp_path: Path) -> None:
    from app.resources.mcp import MCPConnectionError
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [])
    provider = build_capability_provider(tmp_path, config)
    with pytest.raises(MCPConnectionError, match="have not been discovered"):
        await provider.discover(selected_columns=[])
    assert read_records(tmp_path) == []


@pytest.mark.asyncio
async def test_failed_connection_closes_previous_services_and_preserves_cached_preview(tmp_path: Path) -> None:
    from app.resources.mcp import MCPConnectionError
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [], servers=("first", "second"))
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    original_config = config.read_text("utf-8")
    connections = json.loads(original_config)
    connections["mcpServers"]["second"]["command"] = "nonexistent-mcp-executable"
    config.write_text(json.dumps(connections), encoding="utf-8")

    with pytest.raises(MCPConnectionError, match="MCP connection: second"):
        await provider.discover()

    assert provider.preview == initial.catalog
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "first")) == 2
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "second")) == 1
    config.write_text(original_config, encoding="utf-8")
    assert (await provider.discover()).catalog == initial.catalog


@pytest.mark.asyncio
async def test_resource_disconnect_releases_other_services_without_publishing_partial_catalog(tmp_path: Path) -> None:
    from app.resources.mcp import MCPConnectionError
    from mcp_support import read_records, write_mcp_config

    skill_uri = "skill://package/SKILL.md"
    config = write_mcp_config(tmp_path, [], servers=("first", "second"), resources=[
        {"uri": skill_uri, "name": "Package", "mimeType": "text/markdown"},
    ], resource_contents={skill_uri: [{"uri": skill_uri, "text": "---\ndescription: Package\n---"}]})
    provider = build_capability_provider(tmp_path, config)
    initial = await provider.discover()
    scenario_path = tmp_path / "scenario.json"
    scenario = json.loads(scenario_path.read_text("utf-8"))
    scenario["resource_disconnects"] = [skill_uri]
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    with pytest.raises(MCPConnectionError, match="Skill and resource discovery"):
        await provider.discover()

    assert provider.preview == initial.catalog
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path, "second")) == 2
    scenario["resource_disconnects"] = []
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    assert (await provider.discover()).catalog == initial.catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_discovery_releases_connected_processes_and_retains_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    import asyncio
    from app.resources.mcp import MCPConnectionError
    from app.resources.static_resources import SkillResourceProvider
    from mcp_support import read_records, write_mcp_config

    config = write_mcp_config(tmp_path, [])
    provider = build_capability_provider(tmp_path, config, timeout_seconds=10)
    initial = await provider.discover()
    entered, release = asyncio.Event(), asyncio.Event()
    original_discover_resources = SkillResourceProvider.discover_resources

    async def blocked_resources(resources: SkillResourceProvider) -> ResourceCatalogSnapshot:
        entered.set()
        await release.wait()
        return await original_discover_resources(resources)

    monkeypatch.setattr(SkillResourceProvider, "discover_resources", blocked_resources)
    pending = asyncio.create_task(provider.discover())
    try:
        await asyncio.wait_for(entered.wait(), 15)
        if interruption == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(MCPConnectionError, match="TimeoutError"):
                await pending
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert provider.preview == initial.catalog
    assert sum(record["phase"] == "closed" for record in read_records(tmp_path)) == 2
    release.set()
    assert (await provider.discover()).catalog == initial.catalog
