from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import LLMProfileSettings, Settings
from app.container import Container
from app.device.test_fake import FakeDeviceController
from app.domain.activity import AgentActivity
from app.domain.planning import TestTaskType as TaskType
from app.llm import ScriptedChatModelClient
from app.prompts import PromptDefinition
from app.main import create_app


def build_client(
    root: Path,
    *,
    action_timeout_seconds: float = 15,
    settings_overrides: dict[str, Any] | None = None,
) -> TestClient:
    overrides = settings_overrides or {}
    return TestClient(
        create_app(
            Settings(
                data_dir=root,
                database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
                checkpoint_path=root / "checkpoints.db",
                action_timeout_seconds=action_timeout_seconds,
                **overrides,
            )
        )
    )


def create_plan(client: TestClient) -> dict[str, Any]:
    test_case = client.post(
        "/api/test-cases",
        json={"name": "Execution", "source_text": "Verify the current TV screen"},
    ).json()
    response = client.post(
        f"/api/test-cases/{test_case['id']}/plans",
        json={"device_id": "fake-tv"},
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def revise_to_one_task(
    client: TestClient, plan: dict[str, Any], *, max_cycles: int
) -> dict[str, Any]:
    definition = dict(plan["content"]["tasks"][0]["definition"])
    definition["max_cycles"] = max_cycles
    response = client.post(
        f"/api/test-plans/{plan['id']}/revisions",
        json={
            "content": {
                "title": plan["content"]["title"],
                "setup_steps": plan["content"]["setup_steps"],
                "assumptions": plan["content"]["assumptions"],
                "tasks": [definition],
            }
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def install_turns(client: TestClient, turns: list[AIMessage | dict[str, Any] | Exception]) -> None:
    model_client = ScriptedChatModelClient(turns=turns)
    container = _container(client)
    container.model_provider.replace_client_for_testing("default", model_client)


def _container(client: TestClient) -> Container:
    return cast(Container, cast(Any, client.app).state.container)


def start_run(client: TestClient, plan_id: str) -> str:
    response = client.post(
        "/api/runs",
        json={
            "test_plan_id": plan_id,
            "device_id": "fake-tv",
            "assumptions_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for _ in range(300):
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        detail = cast(dict[str, Any], response.json())
        if detail["run"]["status"] == "finished":
            return detail
        time.sleep(0.02)
    raise AssertionError(f"Run did not finish: {detail}")


def run_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/runs/{run_id}/events")
    assert response.status_code == 200, response.text
    return cast(list[dict[str, Any]], response.json()["items"])


def test_failed_task_triggers_global_fail_fast() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = create_plan(client)
            install_turns(
                client, [{"status": "failed", "summary": "Goal is not reachable"}]
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            assert detail["run"]["verdict"] == "FAIL"
            assert [task["status"] for task in detail["task_runs"]] == [
                "failed",
                "skipped",
            ]
            event_types = [item["event"]["type"] for item in run_events(client, run_id)]
            assert event_types.count("tasks.skipped") == 1
            assert event_types[-1] == "run.finished"


def test_invalid_ai_messages_remain_facts_and_end_blocked() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = create_plan(client)
            install_turns(
                client, [AIMessage(content="invalid") for _ in range(3)]
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)
            events = run_events(client, run_id)

            assert detail["run"]["verdict"] == "BLOCKED"
            validation = [
                item for item in events
                if item["event"]["type"] == "message.validation_failed"
            ]
            ai_messages = [
                item for item in events
                if item["event"]["type"] == "message.appended"
                and item["event"]["message"]["role"] == "ai"
            ]
            assert len(validation) == 3
            assert len(ai_messages) == 3
            assert all(item["event"]["message"]["content"] == [
                {"type": "text", "text": "invalid"}
            ] for item in ai_messages)


def test_cycle_guard_uses_existing_screenshot_evidence() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = revise_to_one_task(client, create_plan(client), max_cycles=1)
            install_turns(
                client,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "device_wait",
                                "args": {"duration_ms": 100},
                                "id": "wait-once",
                                "type": "tool_call",
                            }
                        ]
                    }
                ],
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            task = detail["task_runs"][0]
            assert detail["run"]["verdict"] == "FAIL"
            assert task["status"] == "failed"
            assert task["cycle_count"] == 1
            assert task["result"]["reason_code"] == "cycle_limit"
            assert len(task["result"]["evidence_artifact_ids"]) == 1


def test_unknown_state_changing_tool_result_is_not_retried() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = revise_to_one_task(client, create_plan(client), max_cycles=2)
            device = cast(FakeDeviceController, _container(client).devices["fake-tv"])
            calls = 0

            async def fail_after_possible_state_change(_: object) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("result is unknown")

            device.press = fail_after_possible_state_change  # type: ignore[method-assign]
            install_turns(
                client,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "device_press_key",
                                "args": {"key": "DPAD_CENTER"},
                                "id": "press-once",
                                "type": "tool_call",
                            }
                        ]
                    },
                    {"status": "blocked", "summary": "Tool result is unknown"},
                ],
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            assert calls == 1
            assert detail["run"]["verdict"] == "BLOCKED"
            assert detail["task_runs"][0]["status"] == "blocked"


def test_state_changing_tool_timeout_is_not_retried_or_mislabeled() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(
            Path(directory), action_timeout_seconds=0.02
        ) as client:
            plan = revise_to_one_task(client, create_plan(client), max_cycles=2)
            device = cast(FakeDeviceController, _container(client).devices["fake-tv"])
            calls = 0

            async def slow_after_possible_state_change(_: object) -> str:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.2)
                return "unknown"

            device.press = slow_after_possible_state_change  # type: ignore[method-assign]
            install_turns(
                client,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "device_press_key",
                                "args": {"key": "DPAD_CENTER"},
                                "id": "slow-press",
                                "type": "tool_call",
                            }
                        ]
                    }
                ],
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            assert calls == 1
            assert detail["run"]["verdict"] == "BLOCKED"
            assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed", (
                [
                    item["event"]
                    for item in run_events(client, run_id)
                    if item["event"]["type"] == "execution.error"
                ]
            )


def test_capture_and_model_failures_use_bounded_safe_retries() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = create_plan(client)
            device = cast(FakeDeviceController, _container(client).devices["fake-tv"])
            device.capture_failures = 2
            install_turns(
                client,
                [
                    TimeoutError("temporary model failure"),
                    {"status": "passed", "summary": "Recovered"},
                    {"status": "passed", "summary": "Judge passed"},
                ],
            )
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            assert device.capture_failures == 0
            assert detail["run"]["verdict"] == "PASS"
            assert [task["status"] for task in detail["task_runs"]] == [
                "passed",
                "passed",
            ]


def test_cancellation_finishes_with_cancelled_verdict() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = create_plan(client)
            device = cast(FakeDeviceController, _container(client).devices["fake-tv"])
            original_screenshot = device.screenshot
            capture_started = threading.Event()
            release_capture = threading.Event()

            async def slow_screenshot():  # type: ignore[no-untyped-def]
                capture_started.set()
                await asyncio.to_thread(release_capture.wait, 2)
                return await original_screenshot()

            device.screenshot = slow_screenshot  # type: ignore[method-assign]
            run_id = start_run(client, cast(str, plan["id"]))
            assert capture_started.wait(timeout=2)
            response = client.post(f"/api/runs/{run_id}/cancel")
            assert response.status_code == 202, response.text
            assert response.content == b""
            assert "content-type" not in response.headers
            release_capture.set()
            detail = wait_for_run(client, run_id)

            assert detail["run"]["status"] == "finished"
            assert detail["run"]["verdict"] == "CANCELLED"
            assert len(detail["task_runs"]) == 1
            assert detail["task_runs"][0]["status"] == "cancelled"
            assert (
                detail["task_runs"][0]["result"]["reason_code"]
                == "user_cancelled"
            )
            assert run_events(client, run_id)[-1]["event"]["type"] == "run.cancelled"


def test_created_run_uses_snapshot_model_after_activity_routes_change() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        profiles = [
            LLMProfileSettings(id="snapshot"),
            LLMProfileSettings(id="new-route"),
        ]
        with build_client(
            Path(directory),
            settings_overrides={
                "llm_profiles": profiles,
                "planning_model_id": "snapshot",
                "act_model_id": "snapshot",
                "judge_model_id": "snapshot",
            },
        ) as client:
            app = _container(client)
            snapshot_client = ScriptedChatModelClient(
                model_id="snapshot",
                turns=[
                    {"status": "passed", "summary": "snapshot act"},
                    {"status": "passed", "summary": "snapshot judge"},
                ],
            )
            new_route_client = ScriptedChatModelClient(
                model_id="new-route",
                turns=[{"status": "failed", "summary": "wrong route"}],
            )
            app.model_provider.replace_client_for_testing(
                "snapshot", snapshot_client
            )
            app.model_provider.replace_client_for_testing(
                "new-route", new_route_client
            )
            plan = create_plan(client)
            original_start_run = app.repository.start_run
            release_execution = threading.Event()

            async def gated_start_run(test_run_id: str) -> None:
                await asyncio.to_thread(release_execution.wait, 2)
                await original_start_run(test_run_id)

            app.repository.start_run = gated_start_run  # type: ignore[method-assign]
            run_id = start_run(client, cast(str, plan["id"]))
            app.model_provider.replace_activity_route_for_testing(
                AgentActivity.ACT, "new-route"
            )
            app.model_provider.replace_activity_route_for_testing(
                AgentActivity.JUDGE, "new-route"
            )
            release_execution.set()
            detail = wait_for_run(client, run_id)

            assert detail["run"]["verdict"] == "PASS"
            assert detail["snapshot"]["act_model"]["profile_id"] == "snapshot"
            assert detail["snapshot"]["judge_model"]["profile_id"] == "snapshot"
            execution_invocations = [
                invocation
                for invocation in snapshot_client.invocations
                if any(
                    getattr(message, "content", "")
                    in {
                        app.agent_factory.prompts_by_task_type[TaskType.ACT].text,
                        app.agent_factory.prompts_by_task_type[TaskType.JUDGE].text,
                    }
                    for message in invocation
                )
            ]
            assert len(execution_invocations) == 2
            assert new_route_client.invocations == []


def test_explicit_multi_profile_routes_drive_planning_act_and_judge() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        profiles = [
            LLMProfileSettings(id="planner"),
            LLMProfileSettings(id="actor"),
            LLMProfileSettings(id="judge"),
        ]
        with build_client(
            Path(directory),
            settings_overrides={
                "llm_profiles": profiles,
                "planning_model_id": "planner",
                "act_model_id": "actor",
                "judge_model_id": "judge",
            },
        ) as client:
            app = _container(client)
            planner_client = ScriptedChatModelClient(model_id="planner")
            act_client = ScriptedChatModelClient(
                model_id="actor",
                turns=[{"status": "passed", "summary": "acted"}],
            )
            judge_client = ScriptedChatModelClient(
                model_id="judge",
                turns=[{"status": "passed", "summary": "judged"}],
            )
            app.model_provider.replace_client_for_testing(
                "planner", planner_client
            )
            app.model_provider.replace_client_for_testing("actor", act_client)
            app.model_provider.replace_client_for_testing("judge", judge_client)

            plan = create_plan(client)
            assert plan["planning_context"]["planning_model"]["profile_id"] == "planner"
            assert len(planner_client.invocations) == 1
            assert act_client.invocations == []
            assert judge_client.invocations == []

            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)
            assert detail["run"]["verdict"] == "PASS"
            assert detail["snapshot"]["act_model"]["profile_id"] == "actor"
            assert detail["snapshot"]["judge_model"]["profile_id"] == "judge"
            assert len(act_client.invocations) == 1
            assert len(judge_client.invocations) == 1


def test_prompt_text_and_persisted_versions_have_one_content_source() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            app = _container(client)
            model_client = ScriptedChatModelClient(
                turns=[
                    {"status": "passed", "summary": "act"},
                    {"status": "passed", "summary": "judge"},
                ]
            )
            app.model_provider.replace_client_for_testing("default", model_client)
            plan = create_plan(client)
            planner_system_text = cast(str, model_client.invocations[0][0].content)
            assert (
                hashlib.sha256(planner_system_text.encode("utf-8")).hexdigest()[:12]
                == plan["planning_context"]["planning_prompt_version"]
            )

            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)
            execution_system_texts = [
                cast(str, invocation[0].content)
                for invocation in model_client.invocations[1:]
            ]
            expected_versions = [
                detail["snapshot"]["act_prompt_version"],
                detail["snapshot"]["judge_prompt_version"],
            ]
            assert [
                hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                for text in execution_system_texts
            ] == expected_versions


def test_prompt_drift_after_run_creation_blocks_before_model_call() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            app = _container(client)
            model_client = ScriptedChatModelClient()
            app.model_provider.replace_client_for_testing("default", model_client)
            plan = create_plan(client)
            original_start_run = app.repository.start_run
            release_execution = threading.Event()

            async def gated_start_run(test_run_id: str) -> None:
                await asyncio.to_thread(release_execution.wait, 2)
                await original_start_run(test_run_id)

            app.repository.start_run = gated_start_run  # type: ignore[method-assign]
            run_id = start_run(client, cast(str, plan["id"]))
            original_prompt = app.agent_factory.prompts_by_task_type[TaskType.ACT]
            app.agent_factory.prompts_by_task_type[TaskType.ACT] = PromptDefinition(
                name=original_prompt.name,
                text=original_prompt.text + "\nchanged",
                version="changed-version",
            )
            release_execution.set()
            detail = wait_for_run(client, run_id)

            assert detail["run"]["verdict"] == "BLOCKED"
            assert detail["task_runs"][0]["status"] == "blocked"
            assert detail["task_runs"][1]["status"] == "skipped"
            assert (
                detail["task_runs"][0]["result"]["reason_code"]
                == "prompt_version_mismatch"
            )
            assert len(model_client.invocations) == 1


def test_model_client_mapping_drift_after_run_creation_blocks() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            app = _container(client)
            snapshot_client = ScriptedChatModelClient(model_id="default")
            app.model_provider.replace_client_for_testing(
                "default", snapshot_client
            )
            plan = create_plan(client)
            original_start_run = app.repository.start_run
            release_execution = threading.Event()

            async def gated_start_run(test_run_id: str) -> None:
                await asyncio.to_thread(release_execution.wait, 2)
                await original_start_run(test_run_id)

            app.repository.start_run = gated_start_run  # type: ignore[method-assign]
            run_id = start_run(client, cast(str, plan["id"]))
            drifted_client = ScriptedChatModelClient(
                LLMProfileSettings(
                    id="default",
                    model="changed-after-snapshot",
                )
            )
            app.model_provider.replace_client_for_testing(
                "default", drifted_client
            )
            release_execution.set()
            detail = wait_for_run(client, run_id)

            assert detail["run"]["verdict"] == "BLOCKED"
            assert detail["task_runs"][0]["status"] == "blocked"
            assert detail["task_runs"][1]["status"] == "skipped"
            assert (
                detail["task_runs"][0]["result"]["reason_code"]
                == "model_profile_mismatch"
            )
            assert drifted_client.invocations == []


def test_unavailable_device_blocks_without_starting_tasks() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        with build_client(Path(directory)) as client:
            plan = create_plan(client)
            device = cast(FakeDeviceController, _container(client).devices["fake-tv"])
            device.available = False
            run_id = start_run(client, cast(str, plan["id"]))
            detail = wait_for_run(client, run_id)

            assert detail["run"]["verdict"] == "BLOCKED"
            assert detail["task_runs"] == []
            event_types = [item["event"]["type"] for item in run_events(client, run_id)]
            assert "execution.error" in event_types
            assert event_types[-1] == "run.finished"
