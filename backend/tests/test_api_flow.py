from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import Settings
from app.device.test_fake import FakeDeviceController
from app.domain.execution import TaskTerminalDecision
from app.domain.tools import ToolDefinition, ToolResult
from app.graph.planning import PlanningGraph
from app.llm.test_fake import ScriptedChatModelProvider
from app.main import create_app


class BoundaryDevice(FakeDeviceController):
    async def wait(self, duration_ms: int):
        await asyncio.sleep(0.1)
        return await super().wait(100)


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        checkpoint_path=tmp_path / "checkpoints.db",
        llm_mode="scripted",
    )
    return TestClient(create_app(settings))


def set_scripted_turns(client: TestClient, turns: list[object]) -> None:
    provider = ScriptedChatModelProvider(turns=turns)
    container = client.app.state.container
    container.model_provider = provider
    container.planning_graph = PlanningGraph(provider)
    container.agent_factory.model_provider = provider
    container.run_service.model_provider = provider


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
        content="",
        tool_calls=[
            {
                "name": "device_wait",
                "args": {
                    "duration_ms": 100,
                    "decision_summary": "Wait for rendering",
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def press_call(call_id: str = "call-press") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "device_press_key",
                "args": {
                    "key": "DPAD_DOWN",
                    "decision_summary": "Move focus to the target row",
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
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {
                    **arguments,
                    "decision_summary": f"Use {name}",
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


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
        client.app.state.container.devices["fake-tv"].capture_failures = 3
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
        client.app.state.container.devices["fake-tv"] = BoundaryDevice()
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
                TaskTerminalDecision(
                    status="passed",
                    summary="The target screen is visible",
                ),
                TaskTerminalDecision(
                    status="passed",
                    summary="The expected result is visible",
                ),
            ],
        )
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert [task["cycle_count"] for task in run["task_runs"]] == [3, 1]
        assert client.app.state.container.devices["fake-tv"].actions == [
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
                TaskTerminalDecision(
                    status="passed",
                    summary="Act task already reached the target",
                ),
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
        assert client.app.state.container.devices["fake-tv"].actions == []
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        assert sum(item["type"] == "agent.response_invalid" for item in events) == 3
        assert not any(
            item["type"] == "tool.started"
            and item["task_run_id"] == run["task_runs"][1]["id"]
            for item in events
        )


def test_tool_timeout_is_not_replayed_and_forces_a_fresh_observation() -> None:
    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        _, revision = create_case_and_plan(client, source_text="Wait for a slow render")
        set_scripted_turns(
            client,
            [
                wait_call("slow-wait"),
                TaskTerminalDecision(
                    status="passed",
                    summary="The screen recovered after the timeout",
                ),
                TaskTerminalDecision(
                    status="passed",
                    summary="The expected result is visible",
                ),
            ],
        )
        client.app.state.container.devices["fake-tv"] = BoundaryDevice()
        client.app.state.container.agent_factory.action_timeout_seconds = 0.01
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert run["task_runs"][0]["cycle_count"] == 2
        events = client.get(f"/api/runs/{created['id']}/events").json()["items"]
        act_task_id = run["task_runs"][0]["id"]
        assert sum(
            item["type"] == "observation.captured"
            and item["task_run_id"] == act_task_id
            for item in events
        ) == 2
        finished = [
            item
            for item in events
            if item["type"] == "tool.finished"
            and item["task_run_id"] == act_task_id
        ]
        assert len(finished) == 1
        assert finished[0]["payload"]["result"]["status"] == "timed_out"


def test_policies_bind_mutating_tools_only_to_act_and_read_tools_to_both() -> None:
    executed: list[str] = []

    async def record_tool(arguments, context):
        executed.append(f"{context.task_run_id}:{arguments['value']}")
        return ToolResult(success=True, summary="Tool completed")

    with tempfile.TemporaryDirectory(
        ignore_cleanup_errors=True
    ) as directory, build_client(Path(directory)) as client:
        tools = client.app.state.container.tools
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        tools.register(
            ToolDefinition(
                name="mutate_setting",
                description="Change a test setting",
                input_schema=schema,
                changes_device_state=True,
            ),
            record_tool,
        )
        tools.register(
            ToolDefinition(
                name="read_setting",
                description="Read a test setting",
                input_schema=schema,
                changes_device_state=False,
            ),
            record_tool,
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
                TaskTerminalDecision(
                    status="passed",
                    summary="Act completed with the mutating tool",
                ),
                external_tool_call(
                    "read_setting",
                    {"value": "judge"},
                    "judge-read",
                ),
                TaskTerminalDecision(
                    status="passed",
                    summary="Judge completed with the read-only tool",
                ),
            ],
        )
        created = start_run(client, revision)
        run = wait_for_run(client, created["id"])

        assert run["overall_result"] == "PASS"
        assert [item.rsplit(":", 1)[1] for item in executed] == ["act", "judge"]
        assert executed[0].startswith(run["task_runs"][0]["id"])
        assert executed[1].startswith(run["task_runs"][1]["id"])
