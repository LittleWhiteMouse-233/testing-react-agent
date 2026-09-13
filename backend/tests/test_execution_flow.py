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
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from langgraph.types import interrupt

from app.container import Container
from app.artifacts import ArtifactStore
from app.domain.activity import AgentActivity
from app.event_stream.projector import project_run_message
from app.execution.task_agent import TaskAgentGraphState, _TaskRuntime
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


@pytest.mark.parametrize("control_kind", ["bubble", "group", "interrupt"])
def test_unsupported_graph_exit_is_blocked_and_releases_run_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control_kind: str,
) -> None:
    async def raise_control(self: _TaskRuntime, state: TaskAgentGraphState) -> dict[str, object]:
        if control_kind == "interrupt":
            interrupt("unsupported pause")
        if control_kind == "group":
            raise ExceptionGroup("mixed controls", [GraphInterrupt(), OSError("execution failed")])
        raise GraphBubbleUp("unhandled graph control")

    with build_client(tmp_path) as client:
        plan = create_plan(client, task_count=2)
        with monkeypatch.context() as patch:
            patch.setattr(_TaskRuntime, "call_model", raise_control)
            run_id = start_run(client, plan)
            detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert [task["status"] for task in detail["task_runs"]] == ["blocked", "skipped"]
        assert detail["task_runs"][0]["result"]["reason_code"] == "unexpected_error"
        events = client.get(f"/api/runs/{run_id}/events").json()["items"]
        errors = [entry["event"] for entry in events if entry["event"]["type"] == "execution.error"]
        assert len(errors) == 1
        expected_error = {
            "interrupt": "TaskAgent ended without TaskAgentCompletion",
            "bubble": "unhandled graph control",
            "group": "execution failed",
        }[control_kind]
        assert expected_error in errors[0]["message"]
        assert events[-1]["event"]["type"] == "run.finished"

        async def wait_for_cleanup() -> None:
            container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
            work = container.run_service._work
            if work is not None and work.task is not None:
                await asyncio.wait_for(asyncio.shield(work.task), 10)

        assert client.portal is not None
        client.portal.call(wait_for_cleanup)
        assert any(record["phase"] == "closed" for record in read_records(tmp_path))
        # The database and in-memory slot must both admit another run.
        assert wait_for_run(client, start_run(client, plan))["run"]["verdict"] == "PASS"


@pytest.mark.parametrize("with_image,verdict,reason", [(True, "BLOCKED", "cycle_limit"), (False, "BLOCKED", "cycle_limit")])
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


def test_disconnect_stops_without_retry(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(disconnect=True)], turns=[tool_turn(1)],
                      tool_call_timeout_seconds=2) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed", detail["task_runs"][0]["result"]
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


@pytest.mark.parametrize('cooperative', [True, False])
def test_timeout_rebuilds_after_cleanup_or_blocks_cleanup_failure(tmp_path: Path, cooperative: bool) -> None:
    delayed = {**replay_call(delay=3), 'cooperative_cancellation': cooperative}
    with build_client(tmp_path, calls=[replay_call(), delayed],
                      turns=[tool_turn(1), tool_turn(2), finish_turn(3), tool_turn(4), finish_turn(5)],
                      tool_call_timeout_seconds=2) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail['run']['verdict'] == ('PASS' if cooperative else 'BLOCKED'), detail['task_runs'][0]['result']
        if cooperative:
            assert [task['cycle_count'] for task in detail['task_runs']] == [3, 2]
        else:
            assert detail['task_runs'][0]['result']['reason_code'] == 'tool_failed'
            assert detail['task_runs'][1]['status'] == 'skipped'
        report = client.get(f'/api/runs/{run_id}/report').json()
        assert len(report['artifacts']) == (2 if cooperative else 1)  # late image is not accepted
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        if cooperative:
            assert any(isinstance(message, ToolMessage) and 'partially executed' in message.text
                       for invocation in model.invocations for message in invocation)
        else:
            assert 'cleanup failed' in detail['task_runs'][0]['result']['summary']
            denied = client.post('/api/runs', json={
                'test_plan_id': detail['test_plan']['id'], 'assumptions_confirmed': True,
            })
            assert denied.status_code == 409 and denied.json()['code'] == 'run_resources_unavailable'
    records = read_records(tmp_path)
    started = [entry for entry in records if entry['phase'] == 'started']
    assert len(started) == (3 if cooperative else 2)
    assert started[0]['pid'] == started[1]['pid']
    if cooperative:
        assert started[1]['pid'] != started[2]['pid']
    if cooperative:
        assert sum(entry['phase'] == 'closed' for entry in records) == 2
    else:
        # SDK process termination may preempt the remote finally block.
        assert sum(entry['phase'] == 'closed' for entry in records) <= 1
    assert sum(entry['phase'] == 'completed' for entry in records) == 2


@pytest.mark.parametrize('with_image', [False, True])
@pytest.mark.parametrize('max_cycles', [2, 3])
def test_timeout_budget_priority_and_no_unneeded_rebuild(tmp_path: Path, with_image: bool, max_cycles: int) -> None:
    initial = replay_call(content=[image_block()] if with_image else [text_block()])
    with build_client(tmp_path, calls=[initial, replay_call(delay=10)], turns=[tool_turn(1), tool_turn(2)],
                      tool_call_timeout_seconds=2, model_response_max_attempts=1) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, max_cycles=max_cycles)))
        reason = 'cycle_limit' if max_cycles == 2 else 'tool_failed'
        assert detail['task_runs'][0]['result']['reason_code'] == reason
        assert detail['task_runs'][0]['cycle_count'] == 2
        assert detail['run']['verdict'] == 'BLOCKED'
    assert len({entry['pid'] for entry in read_records(tmp_path)}) == 1


def test_failed_rebuild_blocks_without_replaying_call(tmp_path: Path) -> None:
    # Change only the independent replay scenario between processes; the host
    # connection settings and startup tool catalog remain fixed.
    with build_client(tmp_path, calls=[replay_call(delay=10)], turns=[tool_turn(1), tool_turn(2)],
                      tool_call_timeout_seconds=2) as client:
        run_id = start_run(client, create_plan(client))
        for _ in range(300):
            if any(entry['phase'] == 'started' for entry in read_records(tmp_path)):
                break
            time.sleep(0.01)
        (tmp_path / 'scenario.json').write_text('invalid', encoding='utf-8')
        detail = wait_for_run(client, run_id)
        assert detail['task_runs'][0]['result']['reason_code'] == 'tool_failed'
        assert detail['task_runs'][0]['cycle_count'] == 2
    assert sum(entry['phase'] == 'started' for entry in read_records(tmp_path)) == 1


def test_model_can_correct_parameters_after_timeout(tmp_path: Path) -> None:
    first, corrected = tool_turn(1), tool_turn(2)
    first.tool_calls[0]['args'] = {'wait_seconds': 10}
    corrected.tool_calls[0]['args'] = {'wait_seconds': 0}
    delayed = {**replay_call(delay=10), 'arguments': {'wait_seconds': 10}}
    with build_client(tmp_path, calls=[delayed], turns=[first, corrected, finish_turn(3)],
                      tool_call_timeout_seconds=2) as client:
        scenario_path = tmp_path / 'scenario.json'
        scenario = json.loads(scenario_path.read_text(encoding='utf-8'))
        scenario['tools'][0]['inputSchema'] = {'type': 'object', 'properties': {'wait_seconds': {'type': 'integer'}}}
        scenario_path.write_text(json.dumps(scenario), encoding='utf-8')
        run_id = start_run(client, create_plan(client))
        for _ in range(300):
            if any(entry['phase'] == 'started' for entry in read_records(tmp_path)):
                break
            time.sleep(0.01)
        scenario['calls'] = [{**replay_call(), 'arguments': {'wait_seconds': 0}}]
        scenario_path.write_text(json.dumps(scenario), encoding='utf-8')
        detail = wait_for_run(client, run_id)
        assert detail['run']['verdict'] == 'PASS'
        assert detail['task_runs'][0]['cycle_count'] == 3
        assert len(client.get(f'/api/runs/{run_id}/report').json()['artifacts']) == 1
    records = read_records(tmp_path)
    started = [entry for entry in records if entry['phase'] == 'started']
    assert [entry['arguments'] for entry in started] == [{'wait_seconds': 10}, {'wait_seconds': 0}]
    assert started[0]['pid'] != started[1]['pid']
    closed = [entry for entry in records if entry['phase'] == 'closed']
    assert len(closed) == 2
    elapsed = started[1]['time_seconds'] - closed[0]['time_seconds']
    (tmp_path / 'recovery-seconds.json').write_text(json.dumps({'cleanup_to_next_call_seconds': elapsed}))


def test_cancellation_during_rebuild_keeps_run_slot_until_cleanup(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=10)], turns=[tool_turn(1), tool_turn(2)],
                      tool_call_timeout_seconds=4) as client:
        plan = create_plan(client)
        run_id = start_run(client, plan)
        for _ in range(300):
            if any(entry['phase'] == 'started' for entry in read_records(tmp_path)):
                break
            time.sleep(0.01)
        scenario_path = tmp_path / 'scenario.json'
        scenario = json.loads(scenario_path.read_text(encoding='utf-8'))
        scenario['startup_delay_seconds'] = 10
        scenario_path.write_text(json.dumps(scenario), encoding='utf-8')
        for _ in range(800):
            if sum(entry['phase'] == 'opened' for entry in read_records(tmp_path)) == 2:
                break
            time.sleep(0.01)
        assert sum(entry['phase'] == 'opened' for entry in read_records(tmp_path)) == 2
        assert client.post('/api/runs', json={'test_plan_id': plan['id'], 'assumptions_confirmed': True}).status_code == 409
        assert client.post(f'/api/runs/{run_id}/cancel').status_code == 202
        assert wait_for_run(client, run_id)['run']['verdict'] == 'CANCELLED'
    assert sum(entry['phase'] == 'started' for entry in read_records(tmp_path)) == 1


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
    screenshot_contents = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><!--{index}:{"x" * 4096}--></svg>'.encode()
        for index in range(8)
    ]
    screenshot_encodings = [base64.b64encode(content).decode("ascii") for content in screenshot_contents]
    calls: list[dict[str, Any]] = []
    screenshot_offset = 0
    for index in range(1, 6):
        if index == 3:
            calls.append(replay_call(content=[text_block("no image this round")]))
            continue
        calls.append(replay_call(content=[
            text_block(f"before-{index}"),
            {"type": "image", "data": screenshot_encodings[screenshot_offset], "mimeType": "image/svg+xml"},
            text_block(f"after-{index}"),
            {"type": "image", "data": screenshot_encodings[screenshot_offset + 1], "mimeType": "image/svg+xml"},
        ]))
        screenshot_offset += 2
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
        for export_format in ("json", "html"):
            exported = client.post(f"/api/runs/{run_id}/exports", json={"format": export_format}).json()
            download = client.get(f"/api/artifacts/{exported['id']}")
            assert download.status_code == 200
            if export_format == "json":
                assert download.json() == report
            else:
                assert all(f"data:image/svg+xml;base64,{encoded}" in download.text for encoded in screenshot_encodings)
                assert all(artifact["id"] in download.text for artifact in report["artifacts"])
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert sum(block['type'] == 'image' for message in model.invocations[-1] for block in message.content_blocks) == 6
        assert any('outside the visual history' in message.text for message in model.invocations[-1])
        task_id = detail["task_runs"][0]["id"]

    async def checkpoint_messages() -> list[Any]:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            checkpoint = await saver.aget({"configurable": {"thread_id": f"task-run:{task_id}"}})
            assert checkpoint is not None
            messages = checkpoint["channel_values"]["messages"]
            restored = [message.model_copy(deep=True) for message in messages]
            restored_contents: list[bytes] = []
            for message in restored:
                for block in message.content:
                    if not isinstance(block, dict):
                        continue
                    artifact_id = (block.get("extras") or {}).get("artifact_id")
                    if artifact_id is not None:
                        content, mime_type = await container.artifacts.load_content(artifact_id)
                        restored_contents.append(content)
                        block.clear()
                        block.update(type="image", id=artifact_id, mime_type=mime_type,
                                     base64=base64.b64encode(content).decode("ascii"))
            assert restored_contents == screenshot_contents[:2]
            assert [block["base64"] for message in restored for block in message.content_blocks if block["type"] == "image"] == screenshot_encodings
            restored_checkpoint = {**checkpoint, "channel_values": {**checkpoint["channel_values"], "messages": restored}}
            assert len(saver.serde.dumps_typed(checkpoint)[1]) < len(saver.serde.dumps_typed(restored_checkpoint)[1]) - 8_000
            return messages

    messages = asyncio.run(checkpoint_messages())
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 6
    assert sum(block["type"] == "image" for message in messages for block in message.content_blocks) == 6
    assert "outside the visual history" in tool_messages[0].text
    assert "before-1" in tool_messages[0].text and "after-1" in tool_messages[0].text
    assert "base64" not in tool_messages[0].model_dump_json()
    assert [project_run_message(message).model_dump(mode="json") for message in messages] == event_messages
    assert all(message.artifact is None for message in tool_messages)
    assert {message.tool_call_id for message in tool_messages} == {
        call["id"] for message in messages if isinstance(message, AIMessage) for call in message.tool_calls
    }
    assert [block["base64"] for message in messages for block in message.content_blocks if block["type"] == "image"] == screenshot_encodings[2:]


@pytest.mark.parametrize("exit_kind", ["finish", "cancel", "tool_error", "cycle_limit"])
def test_visual_retention_reaches_state_and_terminal_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_kind: str,
) -> None:
    original_after_tools = _TaskRuntime.after_tools
    observed_states: list[list[Any]] = []

    async def observe_after_tools(runtime: _TaskRuntime, state: TaskAgentGraphState) -> dict[str, object]:
        observed_states.append(list(state["messages"]))
        if exit_kind == "cancel" and state["cycle_count"] == 2:
            runtime.run_cancellation_event.set()
        return await original_after_tools(runtime, state)

    monkeypatch.setattr(_TaskRuntime, "after_tools", observe_after_tools)
    with build_client(tmp_path, calls=[replay_call(), replay_call(error=exit_kind == "tool_error")],
                      turns=[tool_turn(1), tool_turn(2), finish_turn(3)],
                      screenshot_history_rounds=1, model_response_max_attempts=1) as client:
        run_id = start_run(client, create_plan(client, max_cycles=2 if exit_kind == "cycle_limit" else 3))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == {
            "finish": "PASS", "cancel": "CANCELLED", "tool_error": "BLOCKED", "cycle_limit": "BLOCKED",
        }[exit_kind]
        task_id = detail["task_runs"][0]["id"]
        if exit_kind == "finish":
            # The next node receives the already-replaced State, independent of
            # model input projection. Earlier node snapshots remain unchanged.
            assert sum(block["type"] == "image" for message in observed_states[-1] for block in message.content_blocks) == 1
        assert sum(block["type"] == "image" for message in observed_states[1] for block in message.content_blocks) == 2
        report = client.get(f"/api/runs/{run_id}/report").json()

    async def verify_checkpoint() -> None:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            checkpoint = await saver.aget({"configurable": {"thread_id": f"task-run:{task_id}"}})
            assert checkpoint is not None
            messages = checkpoint["channel_values"]["messages"]
            assert sum(block["type"] == "image" for message in messages for block in message.content_blocks) == 1
            first_return = next(message for message in messages if isinstance(message, ToolMessage))
            assert "base64" not in first_return.model_dump_json()
            assert "outside the visual history" in first_return.text
            assert [project_run_message(message).model_dump(mode="json") for message in messages] == [
                entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"
            ]

    asyncio.run(verify_checkpoint())


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
def test_exhaustion_during_repair_is_always_blocked(tmp_path: Path, with_image: bool) -> None:
    content = [image_block()] if with_image else [text_block()]
    with build_client(tmp_path, calls=[replay_call(content=content)],
                      turns=[tool_turn(1), AIMessage(content="invalid")], model_response_max_attempts=1) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, max_cycles=2)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "cycle_limit"


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


@pytest.mark.parametrize("failure_kind", ["invalid_base64", "invalid_mime", "first_save", "second_save"])
def test_evidence_failure_preserves_saved_facts_and_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str,
) -> None:
    second_image = image_block()
    if failure_kind == "invalid_base64":
        second_image["data"] = "not-base64!"
    elif failure_kind == "invalid_mime":
        second_image["mimeType"] = "text/plain"
    content = [text_block("before"), image_block(), text_block("between"), second_image, text_block("after")]
    call = replay_call(content=content)
    call["result"]["structuredContent"] = {"preview": second_image["data"], "count": 2}
    original_save = ArtifactStore.save_screenshot
    save_attempts = 0

    async def save_screenshot(store: ArtifactStore, **kwargs):
        nonlocal save_attempts
        save_attempts += 1
        if ((failure_kind == "first_save" and save_attempts == 1)
                or (failure_kind == "second_save" and save_attempts == 2)):
            raise OSError("screenshot storage unavailable")
        return await original_save(store, **kwargs)

    monkeypatch.setattr(ArtifactStore, "save_screenshot", save_screenshot)
    with build_client(tmp_path, calls=[call], turns=[tool_turn(1), finish_turn(2)]) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        assert detail["task_runs"][1]["status"] == "skipped"
        report = client.get(f"/api/runs/{run_id}/report").json()
        expected_images = 0 if failure_kind == "first_save" else 1
        assert len(report["artifacts"]) == expected_images
        events = [entry["event"] for entry in report["events"]]
        returned = [event["message"] for event in events
                    if event["type"] == "message.appended" and event["message"]["role"] == "tool"]
        assert not returned  # application failures are execution events, not retryable tool messages
        errors = [event for event in events if event["type"] == "execution.error"]
        assert len(errors) == 1 and errors[0]["reason_code"] == "tool_failed"
        assert PNG_BASE64 not in json.dumps(events)
        for artifact in report["artifacts"]:
            assert client.get(f"/api/artifacts/{artifact['id']}").status_code == 200
    assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == 1


def test_slow_evidence_save_is_outside_tool_timeout_and_precedes_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_save = ArtifactStore.save_screenshot
    observed_unsaved_events: list[dict[str, Any]] = []

    with build_client(tmp_path, calls=[replay_call()], turns=[tool_turn(1), finish_turn(2)],
                      tool_call_timeout_seconds=2) as client:
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_append = container.repository.append_events

        async def append_events(events):
            for event, _ in events:
                if event.type == "message.appended" and event.message.role == "tool":
                    for block in event.message.content:
                        if block.type == "image_artifact":
                            content, _ = await container.artifacts.load_content(block.artifact_id)
                            assert content == base64.b64decode(PNG_BASE64)
            return await original_append(events)

        async def save_screenshot(store: ArtifactStore, **kwargs):
            # The raw ToolNode result must not be published while storage waits.
            runs, _ = await container.repository.list_test_runs(test_case_id=None, limit=1, offset=0)
            events = await container.repository.list_events(runs[0].id)
            observed_unsaved_events.extend(entry.event.model_dump(mode="json") for entry in events)
            await asyncio.sleep(2.1)
            return await original_save(store, **kwargs)

        monkeypatch.setattr(container.repository, "append_events", append_events)
        monkeypatch.setattr(ArtifactStore, "save_screenshot", save_screenshot)
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert not any(event["type"] == "message.appended" and event["message"]["role"] == "tool"
                       for event in observed_unsaved_events)
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}/events").json()["items"]]
        started = next(index for index, event in enumerate(events) if event["type"] == "tool.started")
        returned = next(index for index, event in enumerate(events)
                        if event["type"] == "message.appended" and event["message"]["role"] == "tool")
        finished = next(index for index, event in enumerate(events) if event["type"] == "task.finished")
        assert started < returned < finished


def test_tool_event_write_failure_stops_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path, calls=[replay_call()], turns=[tool_turn(1), finish_turn(2)]) as client:
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_append = container.repository.append_events

        async def append_events(events):
            if any(event.type == "message.appended" and event.message.role == "tool" for event, _ in events):
                raise OSError("event storage unavailable")
            return await original_append(events)

        monkeypatch.setattr(container.repository, "append_events", append_events)
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert "event storage unavailable" in detail["task_runs"][0]["result"]["summary"]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert len(model.invocations) == 2  # planning, then the first execution decision


@pytest.mark.parametrize("recover", [False, True])
def test_tool_correction_budget_is_shared_and_success_resets_it(tmp_path: Path, recover: bool) -> None:
    invalid = tool_turn(1, "unavailable_tool")
    if recover:
        calls = [replay_call(error=True), replay_call(), replay_call(error=True), replay_call()]
        turns = [invalid, tool_turn(2), tool_turn(3), tool_turn(4, "unavailable_tool"),
                 tool_turn(5), tool_turn(6), finish_turn(7)]
    else:
        calls = [replay_call(error=True), replay_call(delay=10)]
        turns = [invalid, tool_turn(2), tool_turn(3), finish_turn(4)]
    with build_client(tmp_path, calls=calls, turns=turns, tool_call_timeout_seconds=2) as client:
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == ("PASS" if recover else "BLOCKED")
        assert detail["task_runs"][0]["cycle_count"] == (7 if recover else 3)
        if not recover:
            assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        events = client.get(f"/api/runs/{run_id}/events").json()["items"]
        assert not any(entry["event"]["type"] == "execution.error" for entry in events)
        returned_errors = [entry for entry in events if entry["event"]["type"] == "message.appended"
                           and entry["event"]["message"]["role"] == "tool"
                           and entry["event"]["message"]["status"] == "error"]
        assert len(returned_errors) == (4 if recover else 3)
    assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == (4 if recover else 2)
