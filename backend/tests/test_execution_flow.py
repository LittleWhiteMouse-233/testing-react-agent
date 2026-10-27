from __future__ import annotations

from app.api import repository as api_repository
from app.api.schemas import TestRunPageQuery as RunPageQuery

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphBubbleUp, GraphInterrupt
from langgraph.types import interrupt

from app.container import Container
from app.artifacts import ArtifactStore
from app.domain.activity import AgentActivity
from app.domain.planning import TestTaskType as TaskType
from app.event_stream.projector import project_run_message
from app.execution.task_agent import TaskAgentGraphState, _TaskRuntime
from app.llm import ScriptedChatModelClient
from app.llm.test_fake import ScriptedChatModel
from app.prompts import PromptCatalog
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
        assert "device_environment" not in detail["environment"]
        assert "execution_model" in detail["environment"]


def test_memory_is_projected_only_from_prior_tasks_in_the_same_run(tmp_path: Path) -> None:
    memory = "A: model TV-X1\nfirmware V2.3.7"
    turns = [
        tool_turn(1), finish_turn(2),
        tool_turn(3), {"status": "passed", "summary": "Read model and firmware at A", "memory": memory},
        tool_turn(5), finish_turn(6),
        tool_turn(7), finish_turn(8),
    ] * 2
    calls = [replay_call(content=[text_block(observation)])
             for observation in ["Focus A", memory, "Focus B", "B: model TV-X1, firmware V2.3.7"] * 2]
    with build_client(tmp_path, calls=calls, turns=turns) as client:
        plan = create_plan(client, task_count=2)
        definitions = [
            {"task_no": task_no, "type": task_type, "title": title, "goal": goal, "success_criteria": [criterion]}
            for task_no, (task_type, title, goal, criterion) in enumerate([
                ("act", "Focus A", "Focus A", "Focus is at A"),
                ("act", "Read A", "Read and remember A model and firmware", "Model and firmware are read and retained"),
                ("act", "Focus B", "Focus B", "Focus is at B"),
                ("judge", "Compare B", "Compare B with the remembered A fields", "Model and firmware match A"),
            ], 1)
        ]
        revision = client.post(f"/api/test-plans/{plan['id']}/revisions",
                               json={"content": {**plan["content"], "tasks": definitions}})
        assert revision.status_code == 201, revision.text
        plan = revision.json()
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        for _ in range(2):
            first_invocation = len(model.invocations)
            detail = wait_for_run(client, start_run(client, plan))
            assert detail["run"]["verdict"] == "PASS"
            task_runs = detail["task_runs"]
            assert [task["result"]["memory"] for task in task_runs] == [None, memory, None, None]
            contexts = [json.loads(invocation[1].text) for invocation in model.invocations[first_invocation:]]
            expected = [{"task_no": 2, "execution_no": 1, "memory": memory}]
            assert [json.loads(context["memory_text"]) for context in contexts[::2]] == [[], [], expected, expected]
            assert all("previous_task_runs" not in context for context in contexts)


def test_memory_image_reads_preserve_original_identity_window_and_exports(tmp_path: Path) -> None:
    memory_values: list[str] = []

    def remember_image(messages: list[BaseMessage]) -> AIMessage:
        reference = next(block["text"] for block in messages[-1].content_blocks
                         if block["type"] == "text" and block["text"].startswith("[Image artifact "))
        memory = f"A baseline <original>\n{reference}"
        memory_values.append(memory)
        reply = finish_turn(2)
        reply.tool_calls[0]["args"]["memory"] = memory
        return reply

    read_count = 0

    def read_baseline(messages: list[BaseMessage]) -> AIMessage:
        nonlocal read_count
        read_count += 1
        memory = json.loads(json.loads(messages[1].text)["memory_text"])[0]["memory"]
        artifact_id = memory.splitlines()[-1].removeprefix("[Image artifact ").removesuffix("]")
        return AIMessage(content="Read the saved baseline", tool_calls=[{
            "name": "read_image_artifact", "args": {"artifact_id": artifact_id}, "id": f"read-{read_count}",
        }])

    with build_client(tmp_path, image_history_rounds=1, calls=[replay_call(), replay_call()],
                      turns=[tool_turn(1), remember_image, read_baseline, tool_turn(4), read_baseline, finish_turn(6)]) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert detail["task_runs"][0]["result"]["memory"] == memory_values[0]
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == 2
        source = next(artifact for artifact in report["artifacts"]
                      if artifact["task_run_id"] == detail["task_runs"][0]["id"])
        assert source["id"] in memory_values[0]
        events = [entry["event"] for entry in report["events"]]
        assert not any(event["type"] == "message.validation_failed" for event in events)
        receipt = next(event["message"] for event in events if event["type"] == "message.appended"
                       and event["task_run_id"] == detail["task_runs"][0]["id"]
                       and event["message"]["role"] == "tool" and event["message"]["name"] == "finish_task")
        assert receipt["content"][0]["text"].endswith(f"\n\nSubmitted memory:\n{memory_values[0]}")
        with client.stream("GET", f"/api/runs/{run_id}/stream?after=0") as stream:
            streamed_events = [json.loads(line.removeprefix("data: ")) for line in stream.iter_lines()
                               if line.startswith("data: ")]
        assert streamed_events == report["events"]
        reads = [event["message"] for event in events if event["type"] == "message.appended"
                 and event["message"]["role"] == "tool" and event["message"]["name"] == "read_image_artifact"]
        assert len(reads) == 2
        for message in reads:
            assert message["status"] == "success"
            assert message["content"] == [{"type": "text", "text": f"[Image artifact {source['id']}]"},
                                           {"type": "image_artifact", "artifact_id": source["id"]}]
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        before_reread = model.invocations[-2]
        assert any("outside the image history" in message.text and source["id"] in message.text for message in before_reread)
        assert not any(block["type"] == "image" and block.get("id") == source["id"]
                       for message in before_reread for block in message.content_blocks)
        assert any(block["type"] == "image" and block.get("id") == source["id"] and block.get("base64") == PNG_BASE64
                   for message in model.invocations[-1] for block in message.content_blocks)
        for export_format in ("json", "html"):
            exported = client.post(f"/api/runs/{run_id}/exports", json={"format": export_format}).json()
            downloaded = client.get(f"/api/artifacts/{exported['id']}")
            if export_format == "json":
                assert downloaded.json()["detail"]["task_runs"][0]["result"]["memory"] == memory_values[0]
                assert downloaded.json()["events"] == report["events"]
            else:
                assert "A baseline &lt;original&gt;" in downloaded.text
                assert "Submitted memory:\nA baseline &lt;original&gt;" in downloaded.text
                assert "data:image/png;base64," in downloaded.text and source["id"] in downloaded.text


@pytest.mark.parametrize("invalid_artifact", ["missing", "previous_run", "export"])
def test_image_read_business_errors_can_be_corrected(tmp_path: Path, invalid_artifact: str) -> None:
    invalid_id = "11111111-1111-4111-8111-111111111111"

    def invalid_read(messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="Read a saved image", tool_calls=[{
            "name": "read_image_artifact", "args": {"artifact_id": invalid_id}, "id": "invalid-read",
        }])

    def corrected_read(messages: list[BaseMessage]) -> AIMessage:
        observation = next(message for message in reversed(messages)
                           if isinstance(message, ToolMessage) and message.name == "fixture_inspect")
        reference = next(block["text"] for block in observation.content_blocks
                         if block["type"] == "text" and block["text"].startswith("[Image artifact "))
        return AIMessage(content="Read this run's observed image", tool_calls=[{
            "name": "read_image_artifact",
            "args": {"artifact_id": reference.removeprefix("[Image artifact ").removesuffix("]")},
            "id": "corrected-read",
        }])

    with build_client(tmp_path, calls=[replay_call(), replay_call()],
                      turns=[tool_turn(1), finish_turn(2), tool_turn(3), invalid_read, corrected_read, finish_turn(6)]) as client:
        plan = create_plan(client)
        previous_run_id = start_run(client, plan)
        wait_for_run(client, previous_run_id)
        if invalid_artifact == "previous_run":
            invalid_id = client.get(f"/api/runs/{previous_run_id}").json()["artifacts"][0]["id"]
        elif invalid_artifact == "export":
            invalid_id = client.post(f"/api/runs/{previous_run_id}/exports", json={"format": "json"}).json()["id"]
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == 1
        events = [entry["event"] for entry in report["events"]]
        assert not any(event["type"] in {"message.validation_failed", "execution.error"} for event in events)
        returned = [event["message"] for event in events if event["type"] == "message.appended"
                    and event["message"]["role"] == "tool" and event["message"]["name"] == "read_image_artifact"]
        assert [message["status"] for message in returned] == ["error", "success"]
        assert returned[0]["tool_call_id"] == "invalid-read"
        assert "current run" in returned[0]["content"][0]["text"]


def test_required_context_over_budget_blocks_without_calling_model(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client, task_count=2)
        tasks = [dict(task["definition"]) for task in plan["content"]["tasks"]]
        tasks[0]["goal"] = "超长任务说明" * 10_000
        response = client.post(
            f"/api/test-plans/{plan['id']}/revisions",
            json={"content": {**plan["content"], "tasks": tasks}},
        )
        assert response.status_code == 201, response.text
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        model_call_count = len(model.invocations)

        detail = wait_for_run(client, start_run(client, response.json()))

        assert detail["run"]["verdict"] == "BLOCKED"
        assert [task["status"] for task in detail["task_runs"]] == ["blocked", "skipped"]
        assert detail["task_runs"][0]["result"]["reason_code"] == "model_context_exceeded"
        assert detail["task_runs"][0]["cycle_count"] == 1
        assert len(model.invocations) == model_call_count


@pytest.mark.parametrize("act_limit,judge_limit,counts", [(1, 3, [1, 0]), (3, 1, [2, 1])])
def test_configured_cycle_limits_by_task_type(
    tmp_path: Path, act_limit: int, judge_limit: int, counts: list[int],
) -> None:
    with build_client(tmp_path, act_max_cycles=act_limit, judge_max_cycles=judge_limit) as client:
        plan = create_plan(client, task_count=2)
        assert all("max_cycles" not in task["definition"] for task in plan["content"]["tasks"])
        invalid_tasks = [dict(task["definition"], max_cycles=99) for task in plan["content"]["tasks"]]
        response = client.post(f"/api/test-plans/{plan['id']}/revisions",
                               json={"content": {**plan["content"], "tasks": invalid_tasks}})
        assert response.status_code == 422
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert [task["cycle_count"] for task in detail["task_runs"]] == counts
        blocked = next(task for task in detail["task_runs"] if task["status"] == "blocked")
        assert blocked["result"]["reason_code"] == "cycle_limit"
        assert detail["environment"]["max_cycles_by_task_type"] == {"act": act_limit, "judge": judge_limit, "goal": 10}
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        contexts = [json.loads(invocation[1].text) for invocation in model.invocations[1:]]
        for context in contexts:
            expected = act_limit if context["task"]["definition"]["type"] == "act" else judge_limit
            assert context["max_cycles"] == expected
            assert "max_cycles" not in context["task"]["definition"]
        container.run_service.max_cycles_by_task_type.update({TaskType.ACT: 50, TaskType.JUDGE: 50})
        stored = client.get(f"/api/runs/{run_id}").json()["detail"]
        assert stored["environment"] == detail["environment"]
        events = client.get(f"/api/runs/{run_id}").json()["events"]
        for task_run in detail["task_runs"]:
            assert [entry["event"]["cycle_count"] for entry in events
                    if entry["event"]["type"] == "cycle.started" and entry["event"]["task_run_id"] == task_run["id"]] == list(
                        range(1, task_run["cycle_count"] + 1)
                    )


def test_act_and_judge_share_tools_and_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bound_tool_names: list[tuple[str, ...]] = []
    original_bind_tools = ScriptedChatModel.bind_tools

    def bind_tools(model: ScriptedChatModel, tools: Any, **kwargs: Any):
        bound_tool_names.append(tuple(tool.name for tool in tools))
        return original_bind_tools(model, tools, **kwargs)

    monkeypatch.setattr(ScriptedChatModel, "bind_tools", bind_tools)
    prompt_catalog = PromptCatalog()
    act_prompt = prompt_catalog.prompt_for_task_type(TaskType.ACT)
    judge_prompt = prompt_catalog.prompt_for_task_type(TaskType.JUDGE)
    goal_prompt = prompt_catalog.prompt_for_task_type(TaskType.GOAL)
    assert act_prompt is not None and judge_prompt is not None and goal_prompt is not None
    with build_client(tmp_path, calls=[replay_call() for _ in range(4)],
                      turns=[tool_turn(1), finish_turn(2), tool_turn(3), finish_turn(4)] * 2) as client:
        plan = create_plan(client, task_count=2)
        for _ in range(2):
            run_id = start_run(client, plan)
            detail = wait_for_run(client, run_id)
            assert detail["run"]["verdict"] == "PASS"
            assert [task["cycle_count"] for task in detail["task_runs"]] == [2, 2]
            snapshots = detail["environment"]["tool_catalog"]["tools"]
            assert [entry["name"] for entry in snapshots] == [
                "fixture_inspect", "load_skill", "read_resource", "list_resources", "read_image_artifact", "finish_task",
            ]
            assert bound_tool_names[-2:] == [tuple(entry["name"] for entry in snapshots)] * 2
            assert [entry["source"] for entry in snapshots] == [
                "fixture", *(["builtin:skill"] * 3), *(["builtin:task_agent"] * 2),
            ]
            assert detail["environment"]["prompt_fingerprints_by_task_type"] == {
                "act": act_prompt.fingerprint, "judge": judge_prompt.fingerprint, "goal": goal_prompt.fingerprint,
            }
            events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
            for task_run in detail["task_runs"]:
                task_events = [event for event in events if event["task_run_id"] == task_run["id"]]
                assert all(event["test_run_id"] == run_id for event in task_events)
                assert [event["cycle_count"] for event in task_events if event["type"] == "cycle.started"] == [1, 2]
                assert [event["call_id"] for event in task_events if event["type"] == "tool.started"] == (
                    ["call-1", "finish-2"] if task_run == detail["task_runs"][0] else ["call-3", "finish-4"]
                )
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert len(model.invocations) == 9  # one planning call, eight execution replies across two runs
        assert [invocation[0].content for invocation in model.invocations[1:]] == [
            act_prompt.text, act_prompt.text, judge_prompt.text, judge_prompt.text,
        ] * 2


@pytest.mark.parametrize("prompt_text,fingerprint", [
    (None, None), ("", "e3b0c44298fc"), ("abc", "ba7816bf8f01"),
])
def test_optional_prompts_preserve_selected_text_and_snapshot_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    prompt_text: str | None, fingerprint: str | None,
) -> None:
    prompt_directory = tmp_path / "prompts"
    prompt_directory.mkdir()
    if prompt_text is not None:
        for activity in AgentActivity:
            (prompt_directory / f"{activity.value}.txt").write_text(prompt_text, encoding="utf-8")
    monkeypatch.setattr(
        "app.container.PromptCatalog",
        lambda: PromptCatalog(prompt_directory=prompt_directory),
    )

    with build_client(tmp_path, turns=[finish_turn(1), finish_turn(2)]) as client:
        plan = create_plan(client, task_count=2)
        assert plan["planning_context"]["planning_prompt_fingerprint"] == fingerprint
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert detail["environment"]["prompt_fingerprints_by_task_type"] == {
            "act": fingerprint, "judge": fingerprint, "goal": fingerprint,
        }
        stored = client.get(f"/api/runs/{run_id}").json()["detail"]
        assert stored["environment"] == detail["environment"]
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert [invocation[0].content for invocation in model.invocations[1:]] == [prompt_text or ""] * 2


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
        events = client.get(f"/api/runs/{run_id}").json()["events"]
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
def test_round_limit_with_or_without_images(tmp_path: Path, with_image: bool, verdict: str, reason: str) -> None:
    content = [image_block()] if with_image else [text_block()]
    with build_client(tmp_path, calls=[replay_call(content=content)], turns=[tool_turn(1)], act_max_cycles=1) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == verdict
        assert detail["task_runs"][0]["result"]["reason_code"] == reason
        assert detail["task_runs"][0]["cycle_count"] == 1


@pytest.mark.parametrize("status,verdict", [("passed", "PASS"), ("failed", "FAIL"), ("blocked", "BLOCKED")])
@pytest.mark.parametrize("task_count", [1, 2])
def test_text_observation_can_finish_without_images(tmp_path: Path, status: str, verdict: str, task_count: int) -> None:
    turns = [turn for index in range(task_count) for turn in
             (tool_turn(index * 2 + 1), finish_turn(index * 2 + 2, status if index == task_count - 1 else "passed"))]
    with build_client(tmp_path, calls=[replay_call(content=[text_block("Observed target state")])] * task_count,
                      turns=turns) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client, task_count=task_count)))
        assert detail["run"]["verdict"] == verdict
        assert [task["cycle_count"] for task in detail["task_runs"]] == [2] * task_count
        report = client.get(f"/api/runs/{detail['run']['id']}").json()
        assert report["artifacts"] == []
        assert not any(entry["event"]["type"] == "message.validation_failed" for entry in report["events"])


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
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == 1
        messages = [entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"]
        returned = next(message for message in messages if message["role"] == "tool" and message["name"] == "fixture_inspect")
        assert returned["status"] == "error"
        assert [block["type"] for block in returned["content"]] == ["text", "text", "image_artifact", "text"]
        assert returned["content"][1]["text"] == f"[Image artifact {returned['content'][2]['artifact_id']}]"
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


def test_disconnect_stops_without_retry(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(disconnect=True)], turns=[tool_turn(1)],
                      tool_call_timeout_seconds=5) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed", detail["task_runs"][0]["result"]
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


@pytest.mark.parametrize("cooperative", [True, False])
def test_timeout_returns_to_model_and_late_images_are_unconfirmed(tmp_path: Path, cooperative: bool) -> None:
    delayed = {**replay_call(delay=10), "cooperative_cancellation": cooperative}
    with build_client(tmp_path, calls=[replay_call(), delayed, replay_call()],
                      turns=[tool_turn(1), tool_turn(2), finish_turn(3), tool_turn(4), finish_turn(5)],
                      tool_call_timeout_seconds=5) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS", detail["task_runs"]
        assert [task["cycle_count"] for task in detail["task_runs"]] == [3, 2]
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == 2
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        timeout = next(message for invocation in model.invocations for message in invocation
                       if isinstance(message, ToolMessage) and "unconfirmed" in message.text)
        assert timeout.tool_call_id == "call-2" and timeout.status == "error"
        returned = [entry["event"]["message"] for entry in report["events"]
                    if entry["event"]["type"] == "message.appended"
                    and entry["event"]["message"]["role"] == "tool"
                    and entry["event"]["message"]["tool_call_id"] == "call-2"]
        assert len(returned) == 1 and returned[0]["status"] == "error"
        assert not any(entry["event"]["type"] == "execution.error" for entry in report["events"])
    started = [entry for entry in read_records(tmp_path) if entry["phase"] == "started"]
    assert len(started) == 3
    assert [entry["index"] for entry in started] == [0, 1, 2]


@pytest.mark.parametrize("with_image", [False, True])
@pytest.mark.parametrize("max_cycles", [2, 3])
def test_timeout_budget_priority_without_replaying_call(tmp_path: Path, with_image: bool, max_cycles: int) -> None:
    initial = replay_call(content=[image_block()] if with_image else [text_block()])
    with build_client(tmp_path, calls=[initial, replay_call(delay=10)], turns=[tool_turn(1), tool_turn(2)],
                      tool_call_timeout_seconds=5, execution_model_response_max_attempts=1, act_max_cycles=max_cycles) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        reason = "cycle_limit" if max_cycles == 2 else "tool_failed"
        assert detail["task_runs"][0]["result"]["reason_code"] == reason
        assert detail["task_runs"][0]["cycle_count"] == 2
        assert detail["run"]["verdict"] == "BLOCKED"
    assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == 2


def test_call_failure_after_timeout_blocks_without_replay(tmp_path: Path) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=10), replay_call(disconnect=True)],
                      turns=[tool_turn(1), tool_turn(2)], tool_call_timeout_seconds=5) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        assert detail["task_runs"][0]["cycle_count"] == 2
    assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == 2


@pytest.mark.parametrize("failure_kind", ["timeout", "parameters"])
def test_model_can_correct_tool_parameters(tmp_path: Path, failure_kind: str) -> None:
    first, corrected = tool_turn(1), tool_turn(2)
    first.tool_calls[0]["args"] = {"wait_seconds": 10 if failure_kind == "timeout" else "invalid"}
    corrected.tool_calls[0]["args"] = {"wait_seconds": 0}
    calls = ([{**replay_call(delay=10), "arguments": {"wait_seconds": 10}}] if failure_kind == "timeout" else [])
    calls.append({**replay_call(), "arguments": {"wait_seconds": 0}})
    with build_client(tmp_path, calls=calls, turns=[first, corrected, finish_turn(3)],
                      tool_call_timeout_seconds=5) as client:
        scenario_path = tmp_path / "scenario.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        scenario["tools"][0]["inputSchema"] = {
            "type": "object", "properties": {"wait_seconds": {"type": "integer"}}, "required": ["wait_seconds"],
        }
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert detail["task_runs"][0]["cycle_count"] == 3
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == 1
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        correction_messages = [message for message in model.invocations[2] if isinstance(message, ToolMessage)]
        assert correction_messages[-1].status == "error" and correction_messages[-1].tool_call_id == "call-1"
    started = [entry for entry in read_records(tmp_path) if entry["phase"] == "started"]
    assert [entry["arguments"] for entry in started] == (
        [{"wait_seconds": 10}, {"wait_seconds": 0}] if failure_kind == "timeout" else [{"wait_seconds": 0}]
    )


@pytest.mark.parametrize("delay", [0.5, 10])
def test_cancel_waits_for_call_or_timeout_and_never_executes_next_call(tmp_path: Path, delay: float) -> None:
    with build_client(tmp_path, calls=[replay_call(delay=delay), replay_call()],
                      turns=[tool_turn(1), tool_turn(2)], tool_call_timeout_seconds=5) as client:
        plan = create_plan(client)
        run_id = start_run(client, plan)
        for _ in range(300):
            if any(entry["phase"] == "started" for entry in read_records(tmp_path)):
                break
            time.sleep(0.01)
        assert any(entry["phase"] == "started" for entry in read_records(tmp_path))
        assert client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True}).status_code == 409
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "CANCELLED"
        assert detail["task_runs"][0]["status"] == "cancelled"
        assert detail["task_runs"][0]["cycle_count"] == 1
        report = client.get(f"/api/runs/{run_id}").json()
        assert len(report["artifacts"]) == (1 if delay < 5 else 0)
        assert not any(entry["event"]["type"] == "execution.error" for entry in report["events"])
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert len(model.invocations) == 2  # planning and the first execution decision
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
        assert client.get(f"/api/runs/{run_id}").json()["detail"] == detail
    records = read_records(tmp_path)
    assert sum(entry["phase"] == "started" for entry in records) == 1
    assert sum(entry["phase"] == "completed" for entry in records) == (1 if delay < 5 else 0)


def test_planning_does_not_connect_to_mcp(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        (tmp_path / "mcp.json").write_text("invalid", encoding="utf-8")
        plan = create_plan(client)
        assert plan["content"]["tasks"]
        assert not read_records(tmp_path)
        response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert response.status_code == 502
        assert response.json()["code"] == "mcp_unavailable"


def test_image_history_preserves_order_text_events_and_artifacts(tmp_path: Path) -> None:
    image_contents = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><!--{index}:{"x" * 4096}--></svg>'.encode()
        for index in range(8)
    ]
    image_encodings = [base64.b64encode(content).decode("ascii") for content in image_contents]
    calls: list[dict[str, Any]] = []
    image_offset = 0
    for index in range(1, 6):
        if index == 3:
            calls.append(replay_call(content=[text_block("no image this round")]))
            continue
        calls.append(replay_call(content=[
            text_block(f"before-{index}"),
            {"type": "image", "data": image_encodings[image_offset], "mimeType": "image/svg+xml"},
            text_block(f"after-{index}"),
            {"type": "image", "data": image_encodings[image_offset + 1], "mimeType": "image/svg+xml"},
        ]))
        image_offset += 2
    with build_client(tmp_path, calls=calls, turns=[*[tool_turn(i) for i in range(1, 6)], finish_turn(6)],
                      image_history_rounds=3) as client:
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        report = client.get(f"/api/runs/{run_id}").json()
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
                assert all(f"data:image/svg+xml;base64,{encoded}" in download.text for encoded in image_encodings)
                assert all(artifact["id"] in download.text for artifact in report["artifacts"])
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert sum(block['type'] == 'image' for message in model.invocations[-1] for block in message.content_blocks) == 6
        assert any('outside the image history' in message.text for message in model.invocations[-1])
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
            assert restored_contents == image_contents[:2]
            assert [block["base64"] for message in restored for block in message.content_blocks if block["type"] == "image"] == image_encodings
            restored_checkpoint = {**checkpoint, "channel_values": {**checkpoint["channel_values"], "messages": restored}}
            assert len(saver.serde.dumps_typed(checkpoint)[1]) < len(saver.serde.dumps_typed(restored_checkpoint)[1]) - 8_000
            return messages

    messages = asyncio.run(checkpoint_messages())
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 6
    assert sum(block["type"] == "image" for message in messages for block in message.content_blocks) == 6
    assert "outside the image history" in tool_messages[0].text
    assert "before-1" in tool_messages[0].text and "after-1" in tool_messages[0].text
    assert "base64" not in tool_messages[0].model_dump_json()
    assert [project_run_message(message).model_dump(mode="json") for message in messages] == event_messages
    assert all(message.artifact is None for message in tool_messages)
    assert {message.tool_call_id for message in tool_messages} == {
        call["id"] for message in messages if isinstance(message, AIMessage) for call in message.tool_calls
    }
    assert [block["base64"] for message in messages for block in message.content_blocks if block["type"] == "image"] == image_encodings[2:]


@pytest.mark.parametrize("exit_kind", ["finish", "cancel", "tool_error", "cycle_limit"])
def test_image_retention_reaches_state_and_terminal_checkpoint(
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
                      image_history_rounds=1, execution_model_response_max_attempts=1, act_max_cycles=2 if exit_kind == "cycle_limit" else 3) as client:
        run_id = start_run(client, create_plan(client))
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
        report = client.get(f"/api/runs/{run_id}").json()

    async def verify_checkpoint() -> None:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            checkpoint = await saver.aget({"configurable": {"thread_id": f"task-run:{task_id}"}})
            assert checkpoint is not None
            messages = checkpoint["channel_values"]["messages"]
            assert sum(block["type"] == "image" for message in messages for block in message.content_blocks) == 1
            first_return = next(message for message in messages if isinstance(message, ToolMessage))
            assert "base64" not in first_return.model_dump_json()
            assert "outside the image history" in first_return.text
            assert [project_run_message(message).model_dump(mode="json") for message in messages] == [
                entry["event"]["message"] for entry in report["events"] if entry["event"]["type"] == "message.appended"
            ]

    asyncio.run(verify_checkpoint())


def test_model_transport_retry_does_not_consume_a_decision_round(tmp_path: Path) -> None:
    with build_client(tmp_path, turns=[TimeoutError("transient"), tool_turn(1), finish_turn(2)]) as client:
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert detail["task_runs"][0]["cycle_count"] == 2
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
        assert [event["cycle_count"] for event in events if event["type"] == "cycle.started"] == [1, 2]
        starts = [index for index, event in enumerate(events) if event["type"] == "cycle.started"]
        replies = [index for index, event in enumerate(events)
                   if event["type"] == "message.appended" and event["message"]["role"] == "ai"]
        assert starts[0] < replies[0] < starts[1] < replies[1]
        assert len([entry for entry in read_records(tmp_path) if entry["phase"] == "started"]) == 1


def test_cycle_is_persisted_while_model_response_is_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]

        async def exercise() -> None:
            model_entered, release_model = asyncio.Event(), asyncio.Event()
            original_bind_tools = ScriptedChatModel.bind_tools

            def bind_tools(model: ScriptedChatModel, tools: Any, **kwargs: Any):
                bound_model = original_bind_tools(model, tools, **kwargs)

                async def invoke(messages):
                    model_entered.set()
                    await release_model.wait()
                    return await bound_model.ainvoke(messages)

                return RunnableLambda(invoke)

            monkeypatch.setattr(ScriptedChatModel, "bind_tools", bind_tools)
            run = await container.run_service.start(test_plan_id=plan["id"], assumptions_confirmed=True)
            work = container.run_service._work
            assert work is not None and work.task is not None
            try:
                await asyncio.wait_for(model_entered.wait(), 10)
                detail = await container.repository.get_test_run_detail(run.id)
                assert detail.task_runs[0].cycle_count == 1
                events = await api_repository.list_events(container.sessions, run.id)
                starts = [entry.event for entry in events if entry.event.type == "cycle.started"]
                assert len(starts) == 1 and starts[0].cycle_count == 1
                assert starts[0].task_run_id == detail.task_runs[0].id
                assert not any(entry.event.type == "message.appended" and entry.event.message.role == "ai"
                               for entry in events)
            finally:
                release_model.set()
                await asyncio.wait_for(work.task, 10)

        assert client.portal is not None
        client.portal.call(exercise)


@pytest.mark.parametrize("failure_kind", ["retry_exhausted", "non_retryable"])
def test_failed_model_call_retains_started_cycle(tmp_path: Path, failure_kind: str) -> None:
    turns = [TimeoutError("model unavailable")] * 3 if failure_kind == "retry_exhausted" else [ValueError("invalid request")]
    with build_client(tmp_path, turns=turns) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert [task["status"] for task in detail["task_runs"]] == ["blocked", "skipped"]
        assert [task["cycle_count"] for task in detail["task_runs"]] == [1, 0]
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
        assert [event["cycle_count"] for event in events if event["type"] == "cycle.started"] == [1]
        assert sum(event["type"] == "execution.error" for event in events) == 1


def test_task_finish_publication_failure_preserves_the_committed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = "Observed the target satisfying the confirmed criteria"
    memory = "The target state was confirmed before publication failed"
    with build_client(tmp_path, turns=[{"status": "passed", "summary": summary, "memory": memory}]) as client:
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_publish = container.run_event_bus.publish

        async def publish(test_run_id: str, sequence: int) -> None:
            events = await api_repository.list_events(container.sessions, test_run_id)
            if events[-1].event.type == "task.finished":
                raise OSError("Task completion publication failed")
            await original_publish(test_run_id, sequence)

        monkeypatch.setattr(container.run_event_bus, "publish", publish)
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert len(detail["task_runs"]) == 1
        task_run = detail["task_runs"][0]
        assert task_run["status"] == "passed" and task_run["cycle_count"] == 1
        assert task_run["result"] == {"reason_code": "completed", "summary": summary, "memory": memory}
        events = client.get(f"/api/runs/{run_id}").json()["events"]
        assert sum(entry["event"]["type"] == "task.finished" for entry in events) == 1
        errors = [entry["event"] for entry in events if entry["event"]["type"] == "execution.error"]
        assert len(errors) == 1 and "Task completion publication failed" in errors[0]["message"]


def test_cancellation_during_cycle_publication_prevents_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        prior_invocations = len(model.invocations)
        original_commit = container.run_event_writer.commit

        async def commit(event, mutation, **kwargs):
            result = await original_commit(event, mutation, **kwargs)
            if event.type == "cycle.started":
                assert container.run_service._work is not None
                container.run_service._work.cancellation.set()
            return result

        monkeypatch.setattr(container.run_event_writer, "commit", commit)
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "CANCELLED"
        assert detail["task_runs"][0]["cycle_count"] == 1
        assert len(model.invocations) == prior_invocations
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
        assert [event["cycle_count"] for event in events if event["type"] == "cycle.started"] == [1]
        assert not any(event["type"] == "tool.started" for event in events)


def test_failed_cycle_transaction_rolls_back_count_and_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with build_client(tmp_path) as client:
        plan = create_plan(client)
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        prior_invocations = len(model.invocations)
        original_stage = container.run_event_writer._stage

        async def stage(session, event, **kwargs):
            row = await original_stage(session, event, **kwargs)
            if event.type == "cycle.started":
                raise OSError("cycle event storage unavailable")
            return row

        monkeypatch.setattr(container.run_event_writer, "_stage", stage)
        run_id = start_run(client, plan)
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["cycle_count"] == 0
        assert len(model.invocations) == prior_invocations
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
        assert not any(event["type"] == "cycle.started" for event in events)
        assert sum(event["type"] == "execution.error" for event in events) == 1


def test_run_tools_ignore_config_changes_and_hold_the_single_run_slot(tmp_path: Path) -> None:
    from mcp_support import write_mcp_config

    with build_client(tmp_path, calls=[replay_call(delay=0.5), replay_call()],
                      turns=[tool_turn(1), tool_turn(2), finish_turn(3), tool_turn(4, "updated_inspect"), finish_turn(5)]) as client:
        plan = create_plan(client)
        run_id = start_run(client, plan)
        (tmp_path / "mcp.json").write_text("invalid", encoding="utf-8")
        conflict = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
        assert conflict.status_code == 409
        assert wait_for_run(client, run_id)["run"]["verdict"] == "PASS"
        write_mcp_config(tmp_path, [replay_call(content=[text_block("New target state")])], servers=("updated",))
        next_detail = wait_for_run(client, start_run(client, plan))
        assert next_detail["run"]["verdict"] == "PASS"
        snapshots = next_detail["environment"]["tool_catalog"]["tools"]
        assert [tool["source"] for tool in snapshots] == [
            "updated", *(["builtin:skill"] * 3), *(["builtin:task_agent"] * 2),
        ]
        assert len({tool["name"] for tool in snapshots}) == len(snapshots) == 6
    records = read_records(tmp_path)
    # Tool and resource capability discovery each close their own session,
    # followed by the two independent business calls.
    assert len({entry["pid"] for entry in records}) == 4
    assert sum(entry["phase"] == "completed" for entry in records) == 2


@pytest.mark.parametrize("with_image", [False, True])
def test_exhaustion_during_repair_is_always_blocked(tmp_path: Path, with_image: bool) -> None:
    content = [image_block()] if with_image else [text_block()]
    with build_client(tmp_path, calls=[replay_call(content=content)],
                      turns=[tool_turn(1), AIMessage(content="invalid")], execution_model_response_max_attempts=1, act_max_cycles=2) as client:
        detail = wait_for_run(client, start_run(client, create_plan(client)))
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
        assert detail["environment"]["tool_catalog"]["tools"][0]["input_schema"]["required"] == ["document", "pages"]
        report = client.get(f"/api/runs/{run_id}").json()
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
        assert client.get(f"/api/runs/{run_id}").json() == report
        exported = client.post(f"/api/runs/{run_id}/exports", json={"format": "json"}).json()
        assert client.get(f"/api/artifacts/{exported['id']}").json() == report


@pytest.mark.parametrize("failure_kind", ["invalid_base64", "invalid_mime", "first_save", "second_save"])
def test_image_failure_preserves_saved_facts_and_blocks(
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
    original_save = ArtifactStore.save_image
    save_attempts = 0

    async def save_image(store: ArtifactStore, **kwargs):
        nonlocal save_attempts
        save_attempts += 1
        if ((failure_kind == "first_save" and save_attempts == 1)
                or (failure_kind == "second_save" and save_attempts == 2)):
            raise OSError("image storage unavailable")
        return await original_save(store, **kwargs)

    monkeypatch.setattr(ArtifactStore, "save_image", save_image)
    with build_client(tmp_path, calls=[call], turns=[tool_turn(1), finish_turn(2)]) as client:
        run_id = start_run(client, create_plan(client, task_count=2))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "BLOCKED"
        assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        assert detail["task_runs"][1]["status"] == "skipped"
        report = client.get(f"/api/runs/{run_id}").json()
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


def test_slow_image_save_is_outside_tool_timeout_and_precedes_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_save = ArtifactStore.save_image
    observed_unsaved_events: list[dict[str, Any]] = []

    with build_client(tmp_path, calls=[replay_call()], turns=[tool_turn(1), finish_turn(2)],
                      tool_call_timeout_seconds=5) as client:
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

        async def save_image(store: ArtifactStore, **kwargs):
            # The raw ToolNode result must not be published while storage waits.
            runs = (await api_repository.list_test_runs(container.sessions, RunPageQuery(limit=1))).items
            events = await api_repository.list_events(container.sessions, runs[0].id)
            observed_unsaved_events.extend(entry.event.model_dump(mode="json") for entry in events)
            await asyncio.sleep(5.1)
            return await original_save(store, **kwargs)

        monkeypatch.setattr(container.repository, "append_events", append_events)
        monkeypatch.setattr(ArtifactStore, "save_image", save_image)
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        assert not any(event["type"] == "message.appended" and event["message"]["role"] == "tool"
                       for event in observed_unsaved_events)
        events = [entry["event"] for entry in client.get(f"/api/runs/{run_id}").json()["events"]]
        started = next(index for index, event in enumerate(events) if event["type"] == "tool.started")
        returned = next(index for index, event in enumerate(events)
                        if event["type"] == "message.appended" and event["message"]["role"] == "tool")
        finished = next(index for index, event in enumerate(events) if event["type"] == "task.finished")
        assert started < returned < finished


@pytest.mark.parametrize("event_stage", ["validation", "started", "result"])
def test_event_write_failure_stops_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_stage: str,
) -> None:
    first_turn = AIMessage(content="invalid") if event_stage == "validation" else tool_turn(1)
    with build_client(tmp_path, calls=[replay_call()], turns=[first_turn, finish_turn(2)]) as client:
        container = cast(Container, client.app.state.container)  # type: ignore[attr-defined]
        original_append_many = container.repository.append_events
        original_append = container.repository.append_event

        async def append_events(events):
            if event_stage == "result" and any(
                event.type == "message.appended" and event.message.role == "tool" for event, _ in events
            ):
                raise OSError("event storage unavailable")
            return await original_append_many(events)

        async def append_event(event, **kwargs):
            if ((event_stage == "validation" and event.type == "message.validation_failed")
                    or (event_stage == "started" and event.type == "tool.started")):
                raise OSError("event storage unavailable")
            return await original_append(event, **kwargs)

        monkeypatch.setattr(container.repository, "append_events", append_events)
        monkeypatch.setattr(container.repository, "append_event", append_event)
        detail = wait_for_run(client, start_run(client, create_plan(client)))
        assert detail["run"]["verdict"] == "BLOCKED"
        assert "event storage unavailable" in detail["task_runs"][0]["result"]["summary"]
        model = cast(ScriptedChatModelClient, container.model_provider.client_for_activity(AgentActivity.EXECUTION))
        assert len(model.invocations) == 2
        assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == (1 if event_stage == "result" else 0)


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
    with build_client(tmp_path, calls=calls, turns=turns, tool_call_timeout_seconds=5) as client:
        run_id = start_run(client, create_plan(client))
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == ("PASS" if recover else "BLOCKED")
        assert detail["task_runs"][0]["cycle_count"] == (7 if recover else 3)
        if not recover:
            assert detail["task_runs"][0]["result"]["reason_code"] == "tool_failed"
        events = client.get(f"/api/runs/{run_id}").json()["events"]
        assert not any(entry["event"]["type"] == "execution.error" for entry in events)
        returned_errors = [entry for entry in events if entry["event"]["type"] == "message.appended"
                           and entry["event"]["message"]["role"] == "tool"
                           and entry["event"]["message"]["status"] == "error"]
        assert len(returned_errors) == (4 if recover else 3)
    assert sum(entry["phase"] == "started" for entry in read_records(tmp_path)) == (4 if recover else 2)
