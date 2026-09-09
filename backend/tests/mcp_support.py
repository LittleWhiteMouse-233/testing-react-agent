"""Host integration fixtures communicate with an independent MCP process only."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import LLMProfileSettings, Settings
from app.container import Container
from app.llm import ScriptedChatModelClient
from app.main import create_app


PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
FAKE_ENTRY = Path(__file__).resolve().parents[2] / "device/fake_mcp/src/fake_mcp/__main__.py"


def image_block() -> dict[str, Any]:
    return {"type": "image", "data": PNG_BASE64, "mimeType": "image/png"}


def text_block(text: str = "Observed expected result") -> dict[str, Any]:
    return {"type": "text", "text": text}


def replay_call(*, name: str = "inspect", content: list[dict[str, Any]] | None = None,
                error: bool = False, delay: float = 0, disconnect: bool = False) -> dict[str, Any]:
    return {"name": name, "arguments": {}, "result": {"content": content if content is not None else [image_block()], "isError": error},
            "delay_seconds": delay, "disconnect": disconnect}


def write_mcp_config(root: Path, calls: list[dict[str, Any]], *, servers: tuple[str, ...] = ("fixture",)) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    scenario = root / "scenario.json"
    names = list(dict.fromkeys(["inspect", *[call["name"] for call in calls]]))
    scenario.write_text(json.dumps({
        "tools": [{"name": name, "description": f"Fixture {name}",
                   "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                   "annotations": {"readOnlyHint": True}} for name in names],
        "calls": calls,
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
    # BaseSettings supports _env_file publicly; Pydantic's generated signature omits it.
    settings = Settings(_env_file=None, llm_profiles=[LLMProfileSettings(id="default", mode="scripted")],  # type: ignore[call-arg]
                        planning_model_id="default", execution_model_id="default",
                        data_dir=root, database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
                        checkpoint_path=root / "checkpoints.db", mcp_config_path=config, **overrides)
    app = create_app(settings)
    # Install the explicitly scripted model after lifespan has assembled the container.
    original_lifespan = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance):
            container = cast(Container, instance.state.container)
            container.model_provider.replace_client_for_testing("default", ScriptedChatModelClient(
                turns=turns if turns is not None else [tool_turn(1), finish_turn(2), tool_turn(3), finish_turn(4)],
            ))
            yield

    app.router.lifespan_context = lifespan
    return TestClient(app)


def create_plan(client: TestClient, *, max_cycles: int = 10, task_count: int = 1) -> dict[str, Any]:
    case = client.post("/api/test-cases", json={"name": "Protocol test", "source_text": "Verify an observed result"}).json()
    plan = client.post(f"/api/test-cases/{case['id']}/plans").json()
    tasks = [dict(entry["definition"], max_cycles=max_cycles) for entry in plan["content"]["tasks"][:task_count]]
    response = client.post(f"/api/test-plans/{plan['id']}/revisions", json={"content": {**plan["content"], "tasks": tasks}})
    assert response.status_code == 201, response.text
    return response.json()


def start_run(client: TestClient, plan: dict[str, Any]) -> str:
    response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    for _ in range(600):
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["run"]["status"] == "finished":
            return detail
        time.sleep(0.02)
    raise AssertionError("Run did not finish")


def read_records(root: Path, server: str = "fixture") -> list[dict[str, Any]]:
    path = root / f"{server}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
