"""Settings HTTP boundaries expose schemas and safe, atomic updates."""
from __future__ import annotations

import json
import asyncio
from functools import partial
from pathlib import Path
from typing import cast
import threading
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import httpx
from langchain_openai import ChatOpenAI

from app.container import Container
from app.config import get_settings
from app.main import create_app
from app.llm import ModelProvider
from config_support import app_config_values, write_app_config
from mcp_support import build_client, create_plan, finish_turn, start_run, wait_for_run


def test_explicit_config_path_owns_both_startup_read_and_settings_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configuration = app_config_values(tmp_path)
    configuration["app_name"] = "Selected configuration"
    config_path = write_app_config(tmp_path, configuration)
    monkeypatch.setenv("TEST_AGENT_CONFIG_PATH", str(tmp_path / "unselected.toml"))
    with TestClient(create_app(get_settings(config_path))) as client:
        snapshot = client.get("/api/settings").json()
        assert snapshot["settings"]["app_name"] == "Selected configuration"
        assert "config_path" not in snapshot["settings"]
        assert "config_path" not in snapshot["settings_schema"]["properties"]
        for profile in snapshot["settings"]["llm"]["profiles"]:
            profile.pop("api_key_configured")
        snapshot["settings"]["app_name"] = "Saved selected configuration"
        response = client.put("/api/settings", json={"version": snapshot["version"], "settings": snapshot["settings"]})
        assert response.status_code == 200, response.text
    assert get_settings(config_path).app_name == "Saved selected configuration"
    assert not (tmp_path / "unselected.toml").exists()


def test_settings_read_and_save_never_return_stored_or_replacement_tokens(tmp_path: Path) -> None:
    configuration = app_config_values(tmp_path)
    configuration["llm"]["profiles"][0]["api_key"] = "old-api-test-secret"
    config_path = write_app_config(tmp_path, configuration)
    with TestClient(create_app(get_settings(config_path))) as client:
        response = client.get("/api/settings")
        assert response.status_code == 200
        assert "old-api-test-secret" not in response.text
        settings = response.json()
        assert settings["settings"]["llm"]["profiles"][0]["api_key_configured"] is True
        draft = settings["settings"]
        for profile in draft["llm"]["profiles"]:
            profile.pop("api_key_configured")
        draft["llm"]["profiles"][0]["api_key"] = "new-api-test-secret"
        draft["execution"]["goal_max_failures"] = 4
        saved = client.put("/api/settings", json={"version": settings["version"], "settings": draft})
        assert saved.status_code == 200, saved.text
        assert "new-api-test-secret" not in saved.text
        assert get_settings(config_path).llm.profiles[0].api_key == "new-api-test-secret"
        assert saved.json()["restart_required"] is False
        stale = client.put("/api/settings", json={"version": settings["version"], "settings": draft})
        assert stale.status_code == 409


def test_invalid_http_configuration_does_not_echo_token_or_change_file(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        settings = client.get("/api/settings")
        assert settings.status_code == 200
        snapshot = settings.json()
        draft = snapshot["settings"]
        draft["llm"]["profiles"][0].pop("api_key_configured")
        draft["llm"]["profiles"][0]["api_key"] = "never-return-http-secret"
        draft["execution"]["goal_max_failures"] = 0
        response = client.put("/api/settings", json={"version": snapshot["version"], "settings": draft})
        assert response.status_code == 422
        assert "never-return-http-secret" not in response.text
        assert response.json()["errors"][0]["loc"] == ["execution", "goal_max_failures"]
        assert client.get("/api/settings").json()["version"] == snapshot["version"]


def test_blank_model_identity_is_rejected_before_publishing_settings(tmp_path: Path) -> None:
    config_path = write_app_config(tmp_path)
    original_file = config_path.read_bytes()
    with TestClient(create_app(get_settings(config_path))) as client:
        snapshot = client.get("/api/settings").json()
        draft = snapshot["settings"]
        draft["llm"]["profiles"][0].pop("api_key_configured")
        draft["llm"]["profiles"][0]["id"] = " "
        draft["llm"].update(planning_model_id=" ", execution_model_id=" ")
        response = client.put("/api/settings", json={"version": snapshot["version"], "settings": draft})
        assert response.status_code == 422, response.text
        assert client.get("/api/settings").json()["version"] == snapshot["version"]
    assert config_path.read_bytes() == original_file


def test_runtime_snapshot_is_nested_and_serialized_without_credentials(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        plan = create_plan(client)
        detail = wait_for_run(client, start_run(client, plan))
        assert detail["environment"]["execution"]["goal_max_failures"] == 10
        assert "max_cycles_by_task_type" not in detail["environment"]
        assert detail["environment"]["execution_protocol_version"] == "11"


@pytest.mark.parametrize("operation", ["pre_planning", "pre_running"])
def test_preparation_of_another_case_rejects_settings_update(tmp_path: Path, operation) -> None:
    with build_client(tmp_path, turns=[finish_turn(1)]) as client:
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        response = client.get("/api/settings")
        assert response.status_code == 200
        snapshot = response.json()
        for profile in snapshot["settings"]["llm"]["profiles"]:
            profile.pop("api_key_configured")
        with container.test_case_lock.hold("other-case", operation):
            rejected = client.put("/api/settings", json={"version": snapshot["version"], "settings": snapshot["settings"]})
        assert rejected.status_code == 409
        assert client.get("/api/settings").json()["version"] == snapshot["version"]


def test_active_run_keeps_old_model_credentials_and_budget_after_model_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with build_client(tmp_path, turns=[finish_turn(1), finish_turn(2)]) as client:
        plan = create_plan(client, task_count=2)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)

        async def exercise() -> None:
            entered, release = asyncio.Event(), asyncio.Event()
            requests: list[tuple[str, str]] = []

            async def respond(request: httpx.Request) -> httpx.Response:
                request_body = json.loads(request.content)
                requests.append((request.headers["authorization"], request_body["model"]))
                if len(requests) == 1:
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={
                    "id": f"reply-{len(requests)}", "object": "chat.completion", "created": 0,
                    "model": request_body["model"], "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": "Observed expected result", "tool_calls": [{
                            "id": f"finish-{len(requests)}", "type": "function", "function": {
                                "name": "finish_task", "arguments": json.dumps({"status": "passed", "summary": "Actual observation meets the confirmed criteria"}),
                            },
                        }],
                    }}],
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model_http:
                monkeypatch.setattr("app.llm.client.ChatOpenAI", partial(ChatOpenAI, http_async_client=model_http))
                monkeypatch.setattr(container.model_provider, "client_for_profile", ModelProvider().client_for_profile)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                    async def save_model(model_id: str, token: str, failures: int) -> None:
                        current = (await api.get("/api/settings")).json()
                        draft = current["settings"]
                        profile = draft["llm"]["profiles"][0]
                        profile.pop("api_key_configured")
                        profile.update(id=model_id, mode="real", model=model_id, api_key=token, base_url="https://model.invalid/v1")
                        draft["llm"].update(planning_model_id=model_id, execution_model_id=model_id)
                        draft["execution"]["goal_max_failures"] = failures
                        response = await api.put("/api/settings", json={"version": current["version"], "settings": draft})
                        assert response.status_code == 200, response.text

                    await save_model("old-model", "old-run-token", 10)
                    first = await container.run_service.start(test_case_id=plan["test_case_id"], test_plan_id=plan["id"], assumptions_confirmed=True)
                    work = container.run_service._work
                    assert work is not None and work.task is not None
                    try:
                        await asyncio.wait_for(entered.wait(), 5)
                        await save_model("new-model", "new-run-token", 2)
                    finally:
                        release.set()
                    await asyncio.wait_for(work.task, 10)
                    old_report = (await api.get(f"/api/runs/{first.id}")).json()
                    assert old_report["detail"]["run"]["verdict"] == "PASS"
                    assert old_report["detail"]["environment"]["execution"]["goal_max_failures"] == 10
                    assert "old-run-token" not in json.dumps(old_report)
                    second = await container.run_service.start(test_case_id=plan["test_case_id"], test_plan_id=plan["id"], assumptions_confirmed=True)
                    next_work = container.run_service._work
                    assert next_work is not None and next_work.task is not None
                    await asyncio.wait_for(next_work.task, 10)
                    new_report = (await api.get(f"/api/runs/{second.id}")).json()
                    assert new_report["detail"]["environment"]["execution"]["goal_max_failures"] == 2
                    assert "new-run-token" not in json.dumps(new_report)
                    assert requests == [("Bearer old-run-token", "old-model")] * 2 + [("Bearer new-run-token", "new-model")] * 2

        assert client.portal is not None
        client.portal.call(exercise)


def test_planning_finishes_with_its_original_model_after_settings_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        case = client.post("/api/test-cases", json={"name": "Planning isolation", "source_text": "Observe target"}).json()
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        original_generate = container.planning_graph.generate

        async def exercise() -> None:
            entered, release = asyncio.Event(), asyncio.Event()

            async def delayed_generate(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original_generate(*args, **kwargs)

            monkeypatch.setattr(container.planning_graph, "generate", delayed_generate)
            planning = asyncio.create_task(container.planning.generate(test_case_id=case["id"], planning_request_id=str(uuid4())))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    snapshot = (await api.get("/api/settings")).json()
                    draft = snapshot["settings"]
                    profile = draft["llm"]["profiles"][0]
                    profile.pop("api_key_configured")
                    profile.update(id="next-model", model="next-deterministic")
                    draft["llm"].update(planning_model_id="next-model", execution_model_id="next-model")
                    saved = await api.put("/api/settings", json={"version": snapshot["version"], "settings": draft})
                    assert saved.status_code == 200, saved.text
                finally:
                    release.set()
                old_plan = await asyncio.wait_for(planning, 10)
                next_plan = await container.planning.generate(test_case_id=case["id"], planning_request_id=str(uuid4()), user_input="Generate using new model")
                assert old_plan.planning_context.planning_model.profile_id == "default"
                assert next_plan.planning_context.planning_model.profile_id == "next-model"
                assert next_plan.planning_context.planning_model.model == "next-deterministic"

        assert client.portal is not None
        client.portal.call(exercise)


def test_settings_save_rejects_new_planning_run_and_competing_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        app = cast(FastAPI, client.app)
        container = cast(Container, app.state.container)
        service = container.settings_service
        entered, release = threading.Event(), threading.Event()
        original_write = service._write_temporary_file

        def blocked_write(contents: str) -> Path:
            entered.set()
            assert release.wait(10)
            return original_write(contents)

        monkeypatch.setattr(service, "_write_temporary_file", blocked_write)

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                snapshot = (await api.get("/api/settings")).json()
                for profile in snapshot["settings"]["llm"]["profiles"]:
                    profile.pop("api_key_configured")
                command = {"version": snapshot["version"], "settings": snapshot["settings"]}
                saving = asyncio.create_task(api.put("/api/settings", json=command))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    requests = [
                        ("/api/settings", command),
                        (f"/api/test-cases/{plan['test_case_id']}/plans", {"planning_request_id": str(uuid4()), "user_input": "new plan"}),
                        ("/api/runs", {"test_case_id": plan["test_case_id"], "test_plan_id": plan["id"], "assumptions_confirmed": True}),
                    ]
                    for path, request_body in requests:
                        rejected = await (api.put(path, json=request_body) if path == "/api/settings" else api.post(path, json=request_body))
                        assert rejected.status_code == 409, rejected.text
                        assert rejected.json()["code"] == "settings_busy"
                finally:
                    release.set()
                    result = await saving
                assert result.status_code == 200
                assert container.run_service._work is None
                assert not container.test_case_lock.has_preparation()

        assert client.portal is not None
        client.portal.call(exercise)


def test_saved_startup_values_do_not_change_current_health_or_cors(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        snapshot = client.get("/api/settings").json()
        draft = snapshot["settings"]
        for profile in draft["llm"]["profiles"]:
            profile.pop("api_key_configured")
        draft.update(app_version="new-version", cors_origins=["https://next.example"])
        saved = client.put("/api/settings", json={"version": snapshot["version"], "settings": draft})
        assert saved.status_code == 200
        assert saved.json()["settings"]["app_version"] == "new-version"
        assert saved.json()["restart_required"] is True
        assert client.get("/health").json()["version"] == "0.1.0"
        for origin, expected_status in (("http://localhost:5173", 200), ("https://next.example", 400)):
            response = client.options("/api/settings", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
            assert response.status_code == expected_status
