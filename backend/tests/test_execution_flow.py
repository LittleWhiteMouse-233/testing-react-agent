from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import Settings
from app.container import Container
from app.device.test_fake import FakeDeviceController
from app.llm.test_fake import ScriptedChatModelProvider
from app.main import create_app


def build_client(root: Path, *, action_timeout_seconds: float = 15) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=root,
                database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
                checkpoint_path=root / "checkpoints.db",
                action_timeout_seconds=action_timeout_seconds,
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
    provider = ScriptedChatModelProvider(turns=turns)
    container = _container(client)
    container.models["default"] = provider


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
            screenshot = device.screenshot

            async def slow_screenshot():  # type: ignore[no-untyped-def]
                await asyncio.sleep(0.1)
                return await screenshot()

            device.screenshot = slow_screenshot  # type: ignore[method-assign]
            run_id = start_run(client, cast(str, plan["id"]))
            response = client.post(f"/api/runs/{run_id}/cancel")
            assert response.status_code == 202, response.text
            assert response.content == b""
            assert "content-type" not in response.headers
            detail = wait_for_run(client, run_id)

            assert detail["run"]["status"] == "finished"
            assert detail["run"]["verdict"] == "CANCELLED"
            assert run_events(client, run_id)[-1]["event"]["type"] == "run.cancelled"


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
