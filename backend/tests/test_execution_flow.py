from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.container import Container
from app.domain.activity import AgentActivity
from app.llm import ScriptedChatModelClient
from mcp_support import (
    PNG_BASE64, build_client, create_plan, finish_turn, image_block, read_records,
    replay_call, start_run, text_block, tool_turn, wait_for_run,
)


@pytest.mark.parametrize("status,verdict", [("passed", "PASS"), ("failed", "FAIL"), ("blocked", "BLOCKED")])
def test_generic_tools_complete_deterministic_verdicts(tmp_path: Path, status: str, verdict: str) -> None:
    with build_client(tmp_path, turns=[tool_turn(1), finish_turn(2, status)]) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, task_count=1 if status == "passed" else 2)))
        assert detail["run"]["verdict"] == verdict
        if status != "passed":
            assert detail["task_runs"][1]["status"] == "skipped"
        assert "device_id" not in detail["run"]
        assert "device_environment" not in detail["snapshot"]
        assert "execution_model" in detail["snapshot"]


def test_act_and_judge_share_tools_and_model(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, task_count=2)))
        assert detail["run"]["verdict"] == "PASS"
        assert [task["cycle_count"] for task in detail["task_runs"]] == [2, 2]
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert len(model.invocations) == 5  # one planning call, four execution replies


@pytest.mark.parametrize("with_image,verdict,reason", [(True, "FAIL", "cycle_limit"), (False, "BLOCKED", "evidence_missing")])
def test_round_limit_and_evidence(tmp_path: Path, with_image: bool, verdict: str, reason: str) -> None:
    content = [image_block()] if with_image else [text_block()]
    with build_client(tmp_path, calls=[replay_call(content=content)], turns=[tool_turn(1)]) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, max_cycles=1)))
        assert detail["run"]["verdict"] == verdict
        assert detail["task_runs"][0]["result"]["reason_code"] == reason
        assert detail["task_runs"][0]["cycle_count"] == 1


def test_finish_without_image_is_repaired_and_counted(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[finish_turn(1), tool_turn(2), finish_turn(3)]) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "PASS"
        assert detail["task_runs"][0]["cycle_count"] == 3
        events = client.get(f"/api/runs/{detail['run']['id']}/events").json()["items"]
        assert sum(entry["event"]["type"] == "message.validation_failed" for entry in events) == 1


def test_invalid_responses_end_blocked_without_implicit_capture(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[AIMessage(content="invalid")] * 3) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["task_runs"][0]["result"]["reason_code"] == "invalid_model_response"
        assert not [entry for entry in read_records(tmp_path) if entry["phase"] == "started"]


def test_tool_errors_keep_partial_images_and_are_not_automatically_replayed(tmp_path: Path) -> None:
    content = [text_block("first result"), image_block(), text_block("later call failed")]
    with build_client(tmp_path, calls=[replay_call(content=content, error=True)],
                      turns=[tool_turn(1), finish_turn(2, "blocked")]) as client:
        run_id = start_run(client, create_plan(client))
        assert wait_for_run(client, run_id)["run"]["verdict"] == "BLOCKED"
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert len(report["artifacts"]) == 1
        messages = [entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"]
        returned = next(message for message in messages if message["role"] == "tool" and message["name"] == "fixture_inspect")
        assert returned["status"] == "error"
        assert [block["type"] for block in returned["content"]] == ["text", "image_artifact", "text"]
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


@pytest.mark.parametrize("disconnect", [False, True])
def test_timeout_or_disconnect_stops_without_retry(tmp_path: Path, disconnect: bool) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=10, disconnect=disconnect)], turns=[tool_turn(1)],
                      tool_call_timeout_seconds=2) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


def test_cancel_inflight_tool_closes_run_without_executing_next_call(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=10), replay_call()], turns=[tool_turn(1), tool_turn(2)]) as client:
        run_id = start_run(client, create_plan(client))
        for _ in range(200):
            if any(entry["phase"] == "started" for entry in read_records(tmp_path)):
                break
            time.sleep(0.02)
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "CANCELLED"
        assert detail["task_runs"][0]["status"] == "cancelled"
    assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1
    assert not [entry for entry in read_records(tmp_path) if entry["phase"] == "completed"]


def test_planning_does_not_connect_to_mcp(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        (tmp_path / "mcp.json").write_text("invalid", encoding="utf-8")
        plan = create_plan(client)
        assert plan["content"]["tasks"]
        assert not read_records(tmp_path)
        response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert response.status_code == 502
        assert response.json()["code"] == "mcp_unavailable"


def test_visual_history_preserves_order_text_events_and_artifacts(tmp_path: Path) -> None:
    calls = [replay_call(content=[text_block(f"before-{index}"), image_block(), text_block(f"after-{index}"), image_block()])
             if index != 3 else replay_call(content=[text_block("no image this round")]) for index in range(1, 6)]
    with build_client(tmp_path, calls=calls, turns=[*[tool_turn(i) for i in range(1, 6)], finish_turn(6)],
                      screenshot_history_rounds=3) as client:
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert len(report["artifacts"]) == 8
        event_messages = [entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"]
        assert len({message["message_id"] for message in event_messages}) == len(event_messages)
        assert sum(block["type"] == "image_artifact" for message in event_messages for block in message["content"]) == 8
        for artifact in report["artifacts"]:
            assert client.get(f"/api/artifacts/{artifact['id']}").status_code == 200
        task_id = detail["task_runs"][0]["id"]

    async def checkpoint_messages() -> list[Any]:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            checkpoint = await saver.aget({"configurable": {"thread_id": f"task-run:{task_id}"}})
            assert checkpoint is not None
            return checkpoint["channel_values"]["messages"]

    messages = asyncio.run(checkpoint_messages())
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 6
    assert sum(block["type"] == "image" for message in messages for block in message.content_blocks) == 4
    assert "outside the visual history" in tool_messages[0].text
    assert "before-1" in tool_messages[0].text and "after-1" in tool_messages[0].text
    assert PNG_BASE64 not in tool_messages[0].model_dump_json()
    assert all(message.artifact is None for message in tool_messages)
    assert {message.tool_call_id for message in tool_messages} == {
        call["id"] for message in messages if isinstance(message, AIMessage) for call in message.tool_calls
    }
    assert all(block["base64"] == PNG_BASE64 for message in messages for block in message.content_blocks if block["type"] == "image")


def test_model_transport_retry_does_not_consume_a_decision_round(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[TimeoutError("transient"), tool_turn(1), finish_turn(2)]) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "PASS"
        assert detail["task_runs"][0]["cycle_count"] == 2
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


def test_running_session_ignores_config_changes_and_holds_the_single_run_slot(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=0.5), replay_call()],
                      turns=[tool_turn(1), tool_turn(2), finish_turn(3)]) as client:
        plan = create_plan(client)
        run_id = start_run(client, plan)
        (tmp_path / "mcp.json").write_text("invalid", encoding="utf-8")
        conflict = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert conflict.status_code == 409
        assert wait_for_run(client, run_id)["run"]["verdict"] == "PASS"
    records = read_records(tmp_path)
    assert len({entry["pid"] for entry in records}) == 1
    assert sum(entry["phase"] == "discovery" for entry in records) == 1
    assert sum(entry["phase"] == "completed" for entry in records) == 2


@pytest.mark.parametrize("with_image", [False, True])
def test_exhaustion_during_repair_uses_evidence_rule(tmp_path: Path, with_image: bool) -> None:
    content = [image_block()] if with_image else [text_block()]
    with build_client(tmp_path, calls=[replay_call(content=content)],
                      turns=[tool_turn(1), AIMessage(content="invalid")], model_response_max_attempts=1) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, max_cycles=2)))
        assert detail["run"]["verdict"] == ("FAIL" if with_image else "BLOCKED")
        assert detail["task_runs"][0]["result"]["reason_code"] == ("cycle_limit" if with_image else "evidence_missing")


def test_custom_schema_structured_result_and_history_without_online_mcp(tmp_path: Path) -> None:
    call = replay_call(name="render_document", content=[text_block("render complete"), image_block()])
    call["arguments"] = {"document": "example", "pages": [2, 4]}
    call["result"]["structuredContent"] = {"pages": 2, "preview": {"bytes": PNG_BASE64}}
    turn = tool_turn(1, "fixture_render_document")
    turn.tool_calls[0]["args"] = call["arguments"]
    with build_client(tmp_path, calls=[call], turns=[turn, finish_turn(2)]) as client:
        scenario_path = tmp_path / "scenario.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        scenario["tools"] = [{"name": "render_document", "description": "Render selected document pages",
                              "inputSchema": {"type": "object", "properties": {
                                  "document": {"type": "string"},
                                  "pages": {"type": "array", "items": {"type": "integer"}},
                              }, "required": ["document", "pages"], "additionalProperties": False}}]
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert detail["snapshot"]["tool_catalog"]["tools"][0]["input_schema"]["required"] == ["document", "pages"]
        report = client.get(f"/api/runs/{run_id}/report").json()
        returned = next(entry["event"]["message"] for entry in report["events"]
                        if entry["event"]["type"] == "message.appended" and entry["event"]["message"].get("name") == turn.tool_calls[0]["name"])
        structured = json.loads(returned["content"][-1]["text"])
        assert structured["pages"] == 2
        assert structured["preview"]["bytes"] == f"[Image artifact {report['artifacts'][0]['id']}]"
        artifact = client.get(f"/api/artifacts/{report['artifacts'][0]['id']}")
        assert artifact.content == base64.b64decode(PNG_BASE64)
    # Refresh/history/export works after the service configuration is unavailable.
    with build_client(tmp_path) as client:
        (tmp_path / "mcp.json").write_text("invalid", encoding="utf-8")
        assert client.get(f"/api/runs/{run_id}/report").json() == report
        exported = client.post(f"/api/runs/{run_id}/exports", json={"format": "json"}).json()
        assert client.get(f"/api/artifacts/{exported['id']}").json() == report
