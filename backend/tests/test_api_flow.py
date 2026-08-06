from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from app.config import LLMProfileSettings, Settings
from app.container import Container
from app.device.test_fake import FakeDeviceController
from app.domain.tools import DeviceCapabilities, ToolEntry
from app.llm.test_fake import ScriptedChatModelProvider
from app.main import create_app
from app.tools import CatalogToolProvider


ScriptedTurn = AIMessage | dict[str, Any] | Exception


class BoundaryDevice(FakeDeviceController):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def wait(self, duration_ms: int):
        self.attempts += 1
        await asyncio.sleep(0.1)
        return await super().wait(100)


class StaticCapabilitySource:
    def __init__(self, entries: tuple[ToolEntry, ...]) -> None:
        self.device_id = "shared-tools"
        self.entries = entries

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            device_id=self.device_id,
            provider="test-shared",
            metadata={},
            tools=self.entries,
        )


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        checkpoint_path=tmp_path / "checkpoints.db",
    )
    return TestClient(create_app(settings))


def get_container(client: TestClient) -> Container:
    app = cast(FastAPI, client.app)
    return cast(Container, app.state.container)


def get_fake_device(client: TestClient) -> FakeDeviceController:
    return cast(
        FakeDeviceController,
        get_container(client).devices["fake-tv"],
    )


def set_scripted_turns(
    client: TestClient, turns: list[ScriptedTurn]
) -> ScriptedChatModelProvider:
    provider = ScriptedChatModelProvider(model_id="default", turns=turns)
    container = get_container(client)
    container.models.clear()
    container.models["default"] = provider
    return provider


def rebuild_tool_catalog(
    client: TestClient,
    *,
    shared_entries: tuple[ToolEntry, ...] = (),
) -> None:
    container = get_container(client)
    shared_sources = (
        (StaticCapabilitySource(shared_entries),) if shared_entries else ()
    )
    catalog = CatalogToolProvider(
        list(container.devices.values()),
        shared_capability_sources=shared_sources,
    )
    container.tools = catalog
    container.agent_factory.tool_provider = catalog
    container.run_service.tools = catalog


def wait_for_run(client: TestClient, run_id: str) -> dict:
    run = {}
    for _ in range(150):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"finished", "cancelled"}:
            return run
        time.sleep(0.03)
    raise AssertionError(f"Run did not finish: {run}")


def create_case_and_plan(
    client: TestClient, name: str = "Firmware", source_text: str = "检查版本"
) -> tuple[dict, dict]:
    case = client.post(
        "/api/test-cases",
        json={"name": name, "source_text": source_text},
    ).json()
    revision = client.post(f"/api/test-cases/{case['id']}/plans", json={}).json()
    return case, revision


def start_run(client: TestClient, revision: dict) -> dict:
    return client.post(
        "/api/runs",
        json={
            "plan_revision_id": revision["id"],
            "device_id": "fake-tv",
            "confirmed_assumptions": revision["plan"]["assumptions"],
        },
    ).json()


def wait_call(call_id: str = "call-wait") -> AIMessage:
    return AIMessage(
        content="Wait for rendering",
        tool_calls=[
            {
                "name": "device_wait",
                "args": {
                    "duration_ms": 100,
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def press_call(call_id: str = "call-press") -> AIMessage:
    return AIMessage(
        content="Move focus to the target row",
        tool_calls=[
            {
                "name": "device_press_key",
                "args": {
                    "key": "DPAD_DOWN",
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def external_tool_call(
    name: str,
    arguments: dict[str, object],
    call_id: str,
) -> AIMessage:
    return AIMessage(
        content=f"Use {name}",
        tool_calls=[
            {
                "name": name,
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_device_health_keeps_description_separate_from_capabilities() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        response = client.get("/api/devices/fake-tv/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["health"] == {
            "available": True,
            "message": "fake device",
        }
        assert payload["description"] == {
            "model": "Fake Android TV",
            "resolution": "1920x1080",
            "locale": "zh-CN",
        }
        assert payload["capabilities"]["metadata"] == {"transport": "fake"}


def test_complete_scripted_run_and_exports() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(
            client,
            source_text="打开设置并检查固件版本号正常显示",
        )
        assert {task["type"] for task in revision["plan"]["tasks"]} == {
            "act",
            "judge",
        }
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])
        assert run["overall_result"] == "PASS"
        assert run["snapshot"]["device"]["health"] == {
            "available": True,
            "message": "fake device",
        }
        assert run["snapshot"]["device"]["description"] == {
            "model": "Fake Android TV",
            "resolution": "1920x1080",
            "locale": "zh-CN",
        }
        assert run["snapshot"]["device"]["capabilities"]["metadata"] == {
            "transport": "fake"
        }
        assert [task["status"] for task in run["task_runs"]] == [
            "passed",
            "passed",
        ]
        assert all(task["outcome"]["evidence_artifact_ids"] for task in run["task_runs"])
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        assert [item["sequence"] for item in events] == list(
            range(1, len(events) + 1)
        )
        assert "agent.terminal_selected" in {item["type"] for item in events}
        report = client.get(f"/api/runs/{created['id']}/report").json()
        assert report["summary"]["passed"] == 2
        assert all(task["evidence"] for task in report["tasks"])
        for export_format in ("json", "html"):
            export = client.post(
                f"/api/runs/{created['id']}/exports",
                json={"format": export_format},
            )
            assert export.status_code == 201
            download = client.get(f"/api/artifacts/{export.json()['id']}")
            assert download.status_code == 200


def test_assumptions_must_match_exactly() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client)
        edited = revision["plan"]
        edited["assumptions"] = ["电视已登录"]
        manual = client.post(
            f"/api/plan-revisions/{revision['id']}/revisions",
            json={"plan": edited},
        ).json()
        rejected = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert rejected.status_code == 422


def test_cycle_guard_fail_fast_and_single_run_guard() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="持续等待")
        plan = revision["plan"]
        plan["tasks"][0]["max_cycles"] = 1
        manual = client.post(
            f"/api/plan-revisions/{revision['id']}/revisions",
            json={"plan": plan},
        ).json()
        set_scripted_turns(client, [wait_call()])
        first = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert first.status_code == 201
        conflict = client.post(
            "/api/runs",
            json={
                "plan_revision_id": manual["id"],
                "device_id": "fake-tv",
                "confirmed_assumptions": [],
            },
        )
        assert conflict.status_code == 409
        run = wait_for_run(client, first.json()["id"])
        assert run["overall_result"] == "FAIL"
        assert run["task_runs"][0]["outcome"]["reason_code"] == "cycle_limit"
        assert run["task_runs"][1]["status"] == "skipped"


def test_capture_failure_becomes_blocked() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="截图失败")
        get_fake_device(client).capture_failures = 3
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])
        assert run["overall_result"] == "BLOCKED"
        assert run["task_runs"][0]["outcome"]["reason_code"] == "capture_failed"
        assert run["task_runs"][1]["status"] == "skipped"


def test_cancel_is_applied_at_tool_boundary() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="等待后取消")
        set_scripted_turns(client, [wait_call()])
        get_container(client).devices["fake-tv"] = BoundaryDevice()
        rebuild_tool_catalog(client)
        created = start_run(client, revision)
        run_id = created["id"]
        for _ in range(100):
            events = client.get(f"/api/runs/{run_id}/events").json()["items"]
            if any(item["type"] == "tool.started" for item in events):
                break
            time.sleep(0.02)
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.json()["cancel_requested"] is True
        run = wait_for_run(client, run_id)
        assert run["overall_result"] == "CANCELLED"
        event_types = {
            item["type"]
            for item in client.get(f"/api/runs/{run_id}/events").json()["items"]
        }
        assert "run.cancelled" in event_types


def test_task_agent_runs_multiple_observe_decide_act_cycles() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="Navigate then verify")
        set_scripted_turns(
            client,
            [
                press_call(),
                wait_call("call-wait-after-press"),
                {"status": "passed", "summary": "The target screen is visible"},
                {"status": "passed", "summary": "The expected result is visible"},
            ],
        )
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert [task["cycle_count"] for task in run["task_runs"]] == [3, 1]
        assert get_fake_device(client).actions == [
            {"type": "PRESS_KEY", "key": "DPAD_DOWN"},
            {"type": "WAIT", "duration_ms": 100},
        ]

        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        act_task_id = run["task_runs"][0]["id"]
        observations = [
            item
            for item in events
            if item["type"] == "observation.captured"
            and item["task_run_id"] == act_task_id
        ]
        assert len(observations) == 3
        assert run["task_runs"][0]["outcome"]["evidence_artifact_ids"] == [
            observations[-1]["payload"]["observation"]["artifact_id"]
        ]

        checkpoint_bytes = (Path(directory) / "checkpoints.db").read_bytes()
        assert b"data:image/png;base64" not in checkpoint_bytes
        assert (
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            not in checkpoint_bytes
        )


def test_judge_policy_rejects_device_state_changing_tools() -> None:
    forbidden = press_call("judge-forbidden")
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="Judge without navigation")
        set_scripted_turns(
            client,
            [
                {"status": "passed", "summary": "Act task already reached the target"},
                forbidden,
                forbidden.model_copy(
                    update={
                        "tool_calls": [
                            {
                                **forbidden.tool_calls[0],
                                "id": "judge-forbidden-2",
                            }
                        ]
                    }
                ),
                forbidden.model_copy(
                    update={
                        "tool_calls": [
                            {
                                **forbidden.tool_calls[0],
                                "id": "judge-forbidden-3",
                            }
                        ]
                    }
                ),
            ],
        )
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "BLOCKED"
        assert run["task_runs"][1]["status"] == "blocked"
        assert (
            run["task_runs"][1]["outcome"]["reason_code"]
            == "invalid_model_response"
        )
        assert get_fake_device(client).actions == []
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        assert sum(item["type"] == "agent.response_invalid" for item in events) == 3
        assert not any(
            item["type"] == "tool.started"
            and item["task_run_id"] == run["task_runs"][1]["id"]
            for item in events
        )


def test_tool_timeout_retries_node_then_repairs_without_observing() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="Wait for a slow render")
        scripted = set_scripted_turns(
            client,
            [
                wait_call("slow-wait"),
                {"status": "passed", "summary": "The screen recovered after the timeout"},
                {"status": "passed", "summary": "The expected result is visible"},
            ],
        )
        boundary = BoundaryDevice()
        get_container(client).devices["fake-tv"] = boundary
        rebuild_tool_catalog(client)
        get_container(client).agent_factory.action_timeout_seconds = 0.01
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert run["task_runs"][0]["cycle_count"] == 1
        assert boundary.attempts == 3
        initial_payload = json.dumps(
            [message.content for message in scripted.invocations[0]],
            default=str,
        )
        repair_payload = json.dumps(
            [message.content for message in scripted.invocations[1]],
            default=str,
        )
        assert "data:image/png;base64" in initial_payload
        assert "data:image/png;base64" not in repair_payload
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        act_task_id = run["task_runs"][0]["id"]
        assert sum(
            item["type"] == "observation.captured"
            and item["task_run_id"] == act_task_id
            for item in events
        ) == 1
        finished = [
            item
            for item in events
            if item["type"] == "tool.finished"
            and item["task_run_id"] == act_task_id
        ]
        assert len(finished) == 1
        assert finished[0]["payload"]["result"]["status"] == "timed_out"


def test_tool_timeout_retry_can_succeed_before_model_repair() -> None:
    attempts = 0

    @tool
    async def eventually_ready() -> str:
        """Wait for a resource that becomes ready."""
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            await asyncio.sleep(0.1)
        return "ready"

    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        rebuild_tool_catalog(
            client,
            shared_entries=(ToolEntry(eventually_ready, frozenset({"act"})),),
        )
        get_container(client).agent_factory.action_timeout_seconds = 0.05
        _, revision = create_case_and_plan(client, source_text="Wait until ready")
        set_scripted_turns(
            client,
            [
                external_tool_call("eventually_ready", {}, "eventually-ready"),
                {"status": "passed", "summary": "The resource is ready"},
                {"status": "passed", "summary": "The result is visible"},
            ],
        )

        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert attempts == 3
        assert run["task_runs"][0]["cycle_count"] == 2
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        finished = [
            item
            for item in events
            if item["type"] == "tool.finished"
            and item["task_run_id"] == run["task_runs"][0]["id"]
        ]
        assert [item["payload"]["result"]["status"] for item in finished] == [
            "succeeded"
        ]


def test_runtime_tool_error_repairs_without_reexecution_or_observation() -> None:
    attempts = 0

    @tool
    async def broken_action(value: str) -> str:
        """Run an action that reports a provider failure."""
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"provider rejected {value}")

    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        rebuild_tool_catalog(
            client,
            shared_entries=(ToolEntry(broken_action, frozenset({"act"})),),
        )
        _, revision = create_case_and_plan(client, source_text="Handle a tool error")
        set_scripted_turns(
            client,
            [
                external_tool_call("broken_action", {"value": "once"}, "broken"),
                {"status": "passed", "summary": "A safe alternative was selected"},
                {"status": "passed", "summary": "The result is visible"},
            ],
        )

        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert attempts == 1
        assert run["task_runs"][0]["cycle_count"] == 1
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        act_task_id = run["task_runs"][0]["id"]
        assert sum(
            item["type"] == "observation.captured"
            and item["task_run_id"] == act_task_id
            for item in events
        ) == 1
        assert next(
            item
            for item in events
            if item["type"] == "tool.finished"
            and item["task_run_id"] == act_task_id
        )["payload"]["result"]["status"] == "blocked"


def test_invalid_tool_schema_repairs_without_invoking_tool() -> None:
    attempts = 0

    @tool
    async def requires_value(value: int) -> str:
        """Use a required integer value."""
        nonlocal attempts
        attempts += 1
        return str(value)

    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        rebuild_tool_catalog(
            client,
            shared_entries=(ToolEntry(requires_value, frozenset({"act"})),),
        )
        _, revision = create_case_and_plan(client, source_text="Repair invalid arguments")
        set_scripted_turns(
            client,
            [
                external_tool_call("requires_value", {}, "invalid-schema"),
                {"status": "passed", "summary": "The invalid action was avoided"},
                {"status": "passed", "summary": "The result is visible"},
            ],
        )

        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert attempts == 0
        assert run["task_runs"][0]["cycle_count"] == 1
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        act_task_id = run["task_runs"][0]["id"]
        assert next(
            item
            for item in events
            if item["type"] == "tool.finished"
            and item["task_run_id"] == act_task_id
        )["payload"]["result"]["status"] == "invalid"
        assert sum(
            item["type"] == "agent.response_invalid"
            and item["task_run_id"] == act_task_id
            for item in events
        ) == 1


def test_policies_bind_mutating_tools_only_to_act_and_read_tools_to_both() -> None:
    executed: list[str] = []

    @tool
    async def mutate_setting(value: str, runtime: ToolRuntime) -> str:
        """Change a test setting."""
        executed.append(f"{runtime.tool_call_id}:{value}")
        return "Tool completed"

    @tool
    async def read_setting(value: str, runtime: ToolRuntime) -> str:
        """Read a test setting."""
        executed.append(f"{runtime.tool_call_id}:{value}")
        return "Tool completed"

    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        rebuild_tool_catalog(
            client,
            shared_entries=(
                ToolEntry(mutate_setting, frozenset({"act"})),
                ToolEntry(read_setting, frozenset({"act", "judge"})),
            ),
        )
        _, revision = create_case_and_plan(client, source_text="Use policy-bound tools")
        set_scripted_turns(
            client,
            [
                external_tool_call(
                    "mutate_setting",
                    {"value": "act"},
                    "act-mutate",
                ),
                {"status": "passed", "summary": "Act completed with the mutating tool"},
                external_tool_call(
                    "read_setting",
                    {"value": "judge"},
                    "judge-read",
                ),
                {"status": "passed", "summary": "Judge completed with the read-only tool"},
            ],
        )
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert [item.rsplit(":", 1)[1] for item in executed] == ["act", "judge"]
        assert executed[0].startswith("act-mutate")
        assert executed[1].startswith("judge-read")


def test_planning_act_and_judge_use_independent_models() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory:
        settings = Settings(
            data_dir=Path(directory),
            database_url=f"sqlite+aiosqlite:///{(Path(directory) / 'app.db').as_posix()}",
            checkpoint_path=Path(directory) / "checkpoints.db",
            llm_profiles=[
                LLMProfileSettings(id="planner"),
                LLMProfileSettings(id="actor"),
                LLMProfileSettings(id="judge"),
            ],
            planning_model_id="planner",
            act_model_id="actor",
            judge_model_id="judge",
        )
        with TestClient(create_app(settings)) as client:
            container = get_container(client)
            container.models["planner"] = ScriptedChatModelProvider(
                model_id="planner"
            )
            container.models["actor"] = ScriptedChatModelProvider(
                model_id="actor",
                turns=[{"status": "passed", "summary": "Actor reached target"}],
            )
            container.models["judge"] = ScriptedChatModelProvider(
                model_id="judge",
                turns=[{"status": "passed", "summary": "Judge verified target"}],
            )

            _, revision = create_case_and_plan(client, source_text="Route models")
            assert revision["model_info"]["profile_id"] == "planner"
            created = start_run(client, revision)
            run = wait_for_run(client, created["id"])

            assert run["overall_result"] == "PASS"
            assert run["snapshot"]["models"]["planning"]["profile_id"] == "planner"
            assert run["snapshot"]["models"]["act"]["profile_id"] == "actor"
            assert run["snapshot"]["models"]["judge"]["profile_id"] == "judge"
            assert run["snapshot"]["models"]["act"] == {
                "profile_id": "actor",
                "provider": "scripted",
                "model": "deterministic",
                "base_url": None,
                "temperature": 0.0,
                "timeout_seconds": 60.0,
            }
            assert run["snapshot"]["execution_protocol_version"] == "1"
