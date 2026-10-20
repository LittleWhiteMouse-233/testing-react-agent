"""TaskAgent timeout feedback and evidence processing contracts."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langsmith import tracing_context

from app.artifacts import ArtifactStore
from app.domain.resources.static_resources import ResourceCatalogSnapshot
from app.domain import planning
from app.domain.errors import ToolCallError, describe_exception
from app.execution.task_agent import TaskAgentFactory, TaskAgentGraphState, TaskAgentInput, _TaskRuntime
from app.execution.task_agent_events import TaskAgentEventRecorder
from app.llm import ModelProvider, ScriptedChatModelClient
from app.prompts import load_prompt
from mcp_support import finish_turn, tool_turn


@pytest.fixture
def task_runtime() -> _TaskRuntime:
    model_client = ScriptedChatModelClient()
    prompt = load_prompt("act")
    return _TaskRuntime(
        artifacts=create_autospec(ArtifactStore, instance=True),
        run_cancellation_event=asyncio.Event(),
        task_input=TaskAgentInput(
            task_run_id=str(uuid4()),
            task=planning.TestTask(test_task_id=str(uuid4()), definition=planning.TestTaskDefinition(
                type=planning.TestTaskType.ACT, title="Observe", goal="Observe the target",
                success_criteria=["Expected content is visible"],
            )),
            previous_task_runs=(), max_cycles=3, screenshot_history_rounds=1,
            expected_model_profile=model_client.profile_snapshot,
            expected_prompt_version=prompt.version,
            resource_catalog=ResourceCatalogSnapshot(),
        ),
        tools=(), model_client=model_client,
        prompt_definition=prompt, model_response_max_attempts=3,
        event_recorder=create_autospec(TaskAgentEventRecorder, instance=True),
    )


def initial_tool_state() -> TaskAgentGraphState:
    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": "observe", "args": {}, "id": "call"}])],
        "cycle_count": 1, "model_response_attempt": 1, "completion": None, "route": "tools",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_cycle", "cycle_start", "invalid_response", "tool_start"])
async def test_cancellation_at_model_or_event_boundary_prevents_further_calls(
    task_runtime: _TaskRuntime, boundary: str,
) -> None:
    calls = 0

    async def observe() -> str:
        nonlocal calls
        calls += 1
        return "observation"

    async def request_cancel(*arguments: object) -> None:
        task_runtime.run_cancellation_event.set()

    event_recorder = create_autospec(TaskAgentEventRecorder, instance=True)
    if boundary == "before_cycle":
        task_runtime.run_cancellation_event.set()
    elif boundary == "cycle_start":
        event_recorder.record_cycle_started.side_effect = request_cancel
    elif boundary == "invalid_response":
        event_recorder.record_message_validation_failed.side_effect = request_cancel
    else:
        event_recorder.record_tool_started.side_effect = request_cancel

    model = ScriptedChatModelClient(turns=[
        AIMessage(content="invalid") if boundary == "invalid_response" else tool_turn(1, "observe"),
    ])
    factory = TaskAgentFactory(
        artifacts=task_runtime.artifacts, model_provider=ModelProvider({model.model_id: model}),
        act_prompt=task_runtime.prompt_definition, judge_prompt=load_prompt("judge"),
        model_response_max_attempts=1,
    )
    graph, initial = await factory.build(
        task_input=task_runtime.task_input,
        tools=(StructuredTool.from_function(coroutine=observe, description="Observe target"),),
        run_cancellation_event=task_runtime.run_cancellation_event, checkpointer=None,
        event_recorder=event_recorder,
    )
    with tracing_context(enabled=False):
        final_state = await graph.ainvoke(initial)
    assert final_state["completion"].status.value == "cancelled"
    assert final_state["completion"].result.reason_code.value == "user_cancelled"
    assert calls == 0
    assert final_state["cycle_count"] == (0 if boundary == "before_cycle" else 1)
    assert final_state["completion"].cycle_count == final_state["cycle_count"]
    assert len(model.invocations) == (0 if boundary in {"before_cycle", "cycle_start"} else 1)


@pytest.mark.asyncio
async def test_native_timeout_returns_to_model_without_waiting_for_late_result(task_runtime: _TaskRuntime) -> None:
    interrupted, release, finished = (asyncio.Event() for _ in range(3))
    calls = 0

    async def observe() -> str:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
            return "LATE RESULT MUST NOT ENTER STATE"
        finally:
            finished.set()
        raise AssertionError("Observation cannot finish before cancellation")

    model = ScriptedChatModelClient(turns=[tool_turn(1, "observe"), finish_turn(2, "blocked")])
    factory = TaskAgentFactory(
        artifacts=task_runtime.artifacts, model_provider=ModelProvider({model.model_id: model}),
        act_prompt=task_runtime.prompt_definition, judge_prompt=load_prompt("judge"),
        tool_call_timeout_seconds=0.05,
    )
    graph, initial = await factory.build(
        task_input=task_runtime.task_input,
        tools=(StructuredTool.from_function(coroutine=observe, description="Observe target"),),
        run_cancellation_event=task_runtime.run_cancellation_event, checkpointer=None,
        event_recorder=task_runtime.event_recorder,
    )
    try:
        with tracing_context(enabled=False):
            final_state = await asyncio.wait_for(graph.ainvoke(initial), 5)
        assert interrupted.is_set() and not finished.is_set()
        assert calls == 1
        returned = next(message for message in model.invocations[1] if isinstance(message, ToolMessage))
        assert returned.tool_call_id == "call-1" and returned.status == "error"
        assert "unconfirmed" in returned.text
        assert final_state["cycle_count"] == 2
        assert final_state["completion"].status.value == "blocked"
        assert "LATE RESULT" not in str(final_state["messages"])
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)


def test_exception_description_preserves_causes_groups_and_suppression() -> None:
    original = OSError("storage offline")
    failure = RuntimeError("report failed")
    failure.__cause__ = original
    failure.__context__ = ValueError("hidden context")
    original.__context__ = failure  # a cycle must not recurse forever
    group = ExceptionGroup("all failures", [failure, *[ValueError(f"branch {i}") for i in range(20)]])
    description = describe_exception(group, phase="Cleanup failed")
    assert description.startswith("Cleanup failed:")
    assert "RuntimeError: report failed" in description and "OSError: storage offline" in description
    assert all(f"ValueError: branch {i}" in description for i in range(20))
    assert "hidden context" not in description and 'File "' not in description
    failure.__cause__ = None
    failure.__suppress_context__ = True
    assert "hidden context" not in describe_exception(failure, phase="Execution failed")


@pytest.mark.asyncio
async def test_all_evidence_processing_failures_are_reported(task_runtime: _TaskRuntime) -> None:
    message = ToolMessage(id="return", tool_call_id="call", content=[
        {"type": "text", "text": "available observation"},
        {"type": "image", "base64": "!invalid!", "mime_type": "image/png"},
    ], artifact={"structured_content": object()})
    with pytest.raises(ToolCallError, match="processing failed") as captured:
        await task_runtime._prepare_tool_result(message)
    assert captured.value.__cause__ is not None
    assert message.artifact is not None  # input snapshots are not mutated


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [None, {"ok": True}])
@pytest.mark.parametrize("already_in_text", [False, True])
async def test_structured_content_projects_once(
    task_runtime: _TaskRuntime, structured: Any, already_in_text: bool,
) -> None:
    message = ToolMessage(id="return", content=json.dumps(structured) if already_in_text else [], tool_call_id="call",
                          artifact={"structured_content": structured})
    prepared = await task_runtime._prepare_tool_result(message)
    assert prepared.id == message.id
    assert len(prepared.content_blocks) == 1
    assert prepared.artifact is None
    assert json.loads(prepared.text) == structured


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_invalid_structured_result_is_terminal_and_keeps_message_identity(
    task_runtime: _TaskRuntime, cancelled: bool,
) -> None:
    original = ToolMessage(id="return", content="Observed text", tool_call_id="call",
                           artifact={"structured_content": {"invalid": object()}})
    state = initial_tool_state()
    state["messages"].append(original)
    if cancelled:
        task_runtime.run_cancellation_event.set()
    with pytest.raises(ToolCallError, match="processing failed"):
        await task_runtime.after_tools(state)
    assert original.status == "success" and original.artifact is not None
