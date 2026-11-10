"""Host integration fixtures communicate with an independent MCP process only."""
from __future__ import annotations

from uuid import uuid4

import json
import sys
import time
from pathlib import Path
from typing import Any
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

from app.llm import ScriptedChatModelClient
from app.main import create_app
from app.resources.mcp import MCPClientProvider, MCPConnectionError
from app.domain.resources import (
    CapabilityCatalog, CapabilityColumnSnapshot, ResourceCatalogSnapshot, ToolCatalogSnapshot,
    build_capability_preview, select_capabilities,
)
from app.resources.static_resources import SkillResourceProvider, ResourceToolContext
from langgraph.prebuilt import ToolRuntime
from config_support import app_config_values, build_settings, install_model_client, write_app_config


PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
FAKE_ENTRY = Path(__file__).resolve().parents[2] / "mcp_servers/fake_mcp/src/fake_mcp_server.py"


def image_block() -> dict[str, Any]:
    return {"type": "image", "data": PNG_BASE64, "mimeType": "image/png"}


def text_block(text: str = "Observed expected result") -> dict[str, Any]:
    return {"type": "text", "text": text}


async def load_mcp_client_provider(config_path: Path) -> MCPClientProvider:
    provider = MCPClientProvider(config_path)
    await provider.load_clients()
    return provider


def replay_call(*, name: str = "inspect", content: list[dict[str, Any]] | None = None,
                error: bool = False, delay: float = 0, disconnect: bool = False) -> dict[str, Any]:
    return {"name": name, "arguments": {}, "result": {"content": content if content is not None else [image_block()], "isError": error},
            "delay_seconds": delay, "disconnect": disconnect}


def write_mcp_config(root: Path, calls: list[dict[str, Any]], *, servers: tuple[str, ...] = ("fixture",),
                     resources: list[dict[str, Any]] | None = None,
                     templates: list[dict[str, Any]] | None = None,
                     resource_contents: dict[str, list[dict[str, Any]]] | None = None,
                     resource_disconnects: list[str] | None = None,
                     tools_enabled: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    scenario = root / "scenario.json"
    names = list(dict.fromkeys(["inspect", *[call["name"] for call in calls]]))
    scenario.write_text(json.dumps({
        "tools": [{"name": name, "description": f"Fixture {name}",
                   "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                   "annotations": {"readOnlyHint": True}} for name in names] if tools_enabled else [],
        "calls": calls,
        "resources": resources or [], "resource_templates": templates or [],
        "resource_contents": resource_contents or {},
        "resource_disconnects": resource_disconnects or [],
    }), encoding="utf-8")
    config = root / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {name: {
        "command": sys.executable, "args": [str(FAKE_ENTRY), "--scenario", str(scenario),
                                            "--record", str(root / f"{name}.jsonl")],
        "cwd": str(root), "env": {"PYTHONUTF8": "1"},
    } for name in servers}}), encoding="utf-8")
    return config


def tool_turn(index: int, name: str = "fixture_inspect") -> AIMessage:
    return AIMessage(content=f"Visual observation / decision {index}", tool_calls=[{
        "name": name, "args": {}, "id": f"call-{index}", "type": "tool_call",
    }])


def finish_turn(index: int, status: str = "passed") -> AIMessage:
    return AIMessage(content="Observed result against the success criteria", tool_calls=[{
        "name": "finish_task", "args": {"status": status, "summary": "Observed result against the success criteria"},
        "id": f"finish-{index}", "type": "tool_call",
    }])


def build_client(root: Path, *, calls: list[dict[str, Any]] | None = None,
                 turns: list[Any] | None = None, **overrides: Any) -> TestClient:
    config = write_mcp_config(root, calls if calls is not None else [replay_call(), replay_call()])
    settings = build_settings(root, mcp_config_path=config, **overrides)
    write_app_config(root, app_config_values(root, mcp_config_path=config, **overrides))
    app = create_app(settings)
    # Install the explicitly scripted model after lifespan has assembled the container.
    original_lifespan = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance):
            scripted_model = ScriptedChatModelClient(
                settings.llm.profiles[0],
                turns=turns if turns is not None else [tool_turn(1), finish_turn(2), tool_turn(3), finish_turn(4)],
            )
            with pytest.MonkeyPatch.context() as model_patch:
                install_model_client(model_patch, scripted_model)
                yield

    app.router.lifespan_context = lifespan
    return TestClient(app)


def create_plan(client: TestClient, *, task_count: int = 1) -> dict[str, Any]:
    case = client.post("/api/test-cases", json={"name": "Protocol test", "source_text": "Verify an observed result"}).json()
    planning = client.post(f"/api/test-cases/{case['id']}/plans", json={"planning_request_id": str(uuid4())})
    assert planning.status_code == 201, planning.text
    plan = planning.json()
    tasks = [entry["definition"] for entry in plan["content"]["tasks"][:task_count]]
    response = client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": {**plan["content"], "tasks": tasks}})
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, plan: dict[str, Any]) -> str:
    response = client.post("/api/runs", json={"test_case_id": plan["test_case_id"], "test_plan_id": plan["id"], "assumptions_confirmed": True,
                                            "selected_columns": capability_selection(client)})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def capability_selection(client: Any) -> list[dict[str, str]]:
    """Simulate a caller selecting its cached preview, without triggering another discovery."""
    if isinstance(client, TestClient):
        # Async lifecycle tests construct requests on the app's event loop thread.
        # Read the same cached preview directly instead of recursively calling its HTTP portal.
        try:
            preview = client.app.state.container.capabilities.preview  # type: ignore[attr-defined]
        except MCPConnectionError:
            return []
        return [column.model_dump(mode="json") for column in preview.columns]
    return client.get("/api/capabilities").json().get("columns", [])


@dataclass(frozen=True)
class ResourceTestContext:
    capability_catalog: CapabilityCatalog[CapabilityColumnSnapshot]


def resource_runtime(
    provider: SkillResourceProvider, selected_ids: set[str] | None = None, *, resource_catalog: ResourceCatalogSnapshot,
) -> ToolRuntime[ResourceToolContext]:
    """Use the production tool-injection contract for independent resource tests."""
    preview = build_capability_preview(ToolCatalogSnapshot(tools=[]), resource_catalog,
                                       [f"mcp:{name}" for name in provider.mcp_clients.clients])
    selections = [column for column in preview.columns if selected_ids is None or column.id in selected_ids]
    return ToolRuntime[ResourceToolContext](state={}, context=ResourceTestContext(select_capabilities(preview, selections)),
                       config={}, stream_writer=lambda _: None, tool_call_id="resource-test", store=None)


async def invoke_resource_tool(
    provider: SkillResourceProvider, name: str, arguments: dict[str, Any], selected_ids: set[str] | None = None,
    *, resource_catalog: ResourceCatalogSnapshot,
) -> ToolMessage:
    """Invoke with the same public ToolNode context injection used by actual TaskAgents."""
    graph = StateGraph(MessagesState, context_schema=ResourceTestContext)
    graph.add_node("tools", ToolNode(provider.build_resource_tools()))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    result = await graph.compile().ainvoke({"messages": [AIMessage(content="", tool_calls=[{
        "name": name, "args": arguments, "id": "resource-test",
    }])]}, context=ResourceTestContext(resource_runtime(provider, selected_ids, resource_catalog=resource_catalog).context.capability_catalog))
    return result["messages"][-1]


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(600):
        detail = client.get(f"/api/runs/{run_id}").json()["detail"]
        if detail["run"]["status"] == "finished":
            return detail
        time.sleep(0.02)
    raise AssertionError("Run did not finish")


def read_records(root: Path, server: str = "fixture") -> list[dict[str, Any]]:
    path = root / f"{server}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
