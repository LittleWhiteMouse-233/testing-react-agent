"""TaskAgent timeout feedback and image processing contracts."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.artifacts import ArtifactStore
from app.domain.resources.static_resources import ResourceCatalogSnapshot
from app.domain import planning
from app.domain.execution import Artifact, ArtifactType, ReasonCode, TaskAgentCompletion, TaskRunResult, TaskRunStatus
from app.domain.errors import ToolCallError, describe_exception
from app.execution.model_context import ModelContextWindow, ModelImageWindow
from app.execution.task_agent import (
    TASK_AGENT_TOOLS, TaskAgentContext, TaskAgentFactory, TaskAgentGraphState,
    TaskAgentInput, TaskAgentRuntimeContext, _TaskRuntime, finish_task,
)
from app.execution.executor import RunExecutor
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.execution.task_agent_events import TaskAgentEventRecorder
from app.execution import task_agent
from app.event_stream.projector import project_run_message
from app.llm import ModelProvider, ScriptedChatModelClient
from app.prompts import PromptCatalog
from mcp_support import PNG_BASE64, finish_turn, tool_turn


@pytest.fixture
def task_runtime() -> _TaskRuntime:
    model_client = ScriptedChatModelClient()
    prompt = PromptCatalog().prompt_for_task_type(planning.TestTaskType.ACT)
    model_tools = (finish_task,)
    task_context = TaskAgentContext(
        task_run_id=str(uuid4()),
        task=planning.TestTask(test_task_id=str(uuid4()), definition=planning.TestTaskDefinition(
            task_no=1,
            type=planning.TestTaskType.ACT, title="Observe", goal="Observe the target",
            success_criteria=["Expected content is visible"],
        )),
        memory_text="[]", max_cycles=3,
    )
    return _TaskRuntime(
        runtime_context=TaskAgentRuntimeContext(
            task_context=task_context, artifacts=create_autospec(ArtifactStore, instance=True),
        ),
        run_cancellation_event=asyncio.Event(),
        tools=model_tools, model_client=model_client,
        available_skills=[],
        model_context_window=ModelContextWindow(model_profile=model_client.profile_snapshot, tools=model_tools),
        model_image_window=ModelImageWindow(history_rounds=1),
        prompt_definition=prompt, model_response_max_attempts=3,
        event_recorder=create_autospec(TaskAgentEventRecorder, instance=True),
    )


def initial_tool_state() -> TaskAgentGraphState:
    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": "observe", "args": {}, "id": "call"}])],
        "cycle_count": 1, "model_response_attempt": 1, "completion": None, "route": "tools",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [None, "", " \t\n", "  A: <original>\nfirmware V2.3.7  "])
async def test_finish_tool_receipt_preserves_only_effective_submitted_memory(memory: str | None) -> None:
    receipt = await finish_task.ainvoke({"status": "passed", "summary": "Observed A", "memory": memory})
    expected = "passed: Observed A"
    if memory and memory.strip():
        expected += f"\n\nSubmitted memory:\n{memory}"
    assert receipt == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration_change", ["missing", "extra"])
async def test_message_publication_contract_is_checked_during_build(
    task_runtime: _TaskRuntime, monkeypatch: pytest.MonkeyPatch, declaration_change: str,
) -> None:
    declared_nodes = frozenset({"initialize", "cycle_guard", "model", "validate", "announce_tool", "after_tools"})
    declared_nodes = (declared_nodes - {"initialize"} if declaration_change == "missing"
                      else declared_nodes | {"unexpected"})
    monkeypatch.setattr(task_agent, "MESSAGE_PUBLISHING_NODES", declared_nodes, raising=False)
    model = ScriptedChatModelClient()
    factory = TaskAgentFactory(
        artifacts=task_runtime.runtime_context.artifacts, model_provider=ModelProvider({model.model_id: model}),
        prompt_catalog=PromptCatalog(),
    )
    expected_difference = r"missing=\['initialize'\]" if declaration_change == "missing" else r"extra=\['unexpected'\]"
    with pytest.raises(ValueError, match=expected_difference):
        await factory.build(
            task_input=TaskAgentInput(
                task_context=task_runtime.runtime_context.task_context, tools=TASK_AGENT_TOOLS,
                resource_catalog=ResourceCatalogSnapshot(), model_profile_id=model.profile_snapshot.profile_id,
            ),
            run_cancellation_event=asyncio.Event(), checkpointer=None, event_recorder=task_runtime.event_recorder,
        )
    assert model.invocations == []


def test_image_tool_snapshot_contains_only_model_visible_arguments() -> None:
    from app.tools import snapshot_tools

    snapshots = snapshot_tools(task_agent.TASK_AGENT_TOOLS)
    image_tool = next(entry for entry in snapshots.tools if entry.name == "read_image_artifact")
    assert image_tool.source == "builtin:task_agent"
    assert set(image_tool.input_schema["properties"]) == {"artifact_id"}
    assert image_tool.input_schema["required"] == ["artifact_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("serialized", [False, True])
async def test_executor_captures_completion_independently_and_waits_for_stream_end(
    task_runtime: _TaskRuntime, serialized: bool,
) -> None:
    completion = TaskAgentCompletion(
        status=TaskRunStatus.PASSED,
        result=TaskRunResult(reason_code=ReasonCode.COMPLETED, summary="Observed target"), cycle_count=1,
    )
    tail_reached = asyncio.Event()

    async def complete(state: TaskAgentGraphState) -> dict[str, object]:
        return {"completion": completion.model_dump(mode="json") if serialized else completion}

    async def publish_tail(state: TaskAgentGraphState) -> dict[str, object]:
        tail_reached.set()
        return {"messages": [AIMessage(id=str(uuid4()), content="Published before stream exit")]}

    graph = StateGraph(TaskAgentGraphState, context_schema=TaskAgentRuntimeContext)
    graph.add_node("completion_only", complete)
    graph.add_node("model", publish_tail)
    graph.add_edge(START, "completion_only")
    graph.add_edge("completion_only", "model")
    graph.add_edge("model", END)
    initial: TaskAgentGraphState = {
        "messages": [], "cycle_count": 0, "model_response_attempt": 0, "completion": None, "route": "model",
    }
    factory = create_autospec(TaskAgentFactory, instance=True)
    checkpointer = InMemorySaver()
    factory.build.return_value = (graph.compile(checkpointer=checkpointer), initial, task_runtime.runtime_context)
    repository = create_autospec(SqlAlchemyRunRepository, instance=True)
    executor = RunExecutor(repository=repository, agent_factory=factory, checkpoint_path="unused", goal_max_failures=10)
    with tracing_context(enabled=False):
        actual = await executor._execute_task(
            test_run_id=str(uuid4()),
            task_input=TaskAgentInput(
                task_context=task_runtime.runtime_context.task_context, tools=TASK_AGENT_TOOLS,
                resource_catalog=ResourceCatalogSnapshot(), model_profile_id=task_runtime.model_client.profile_snapshot.profile_id,
            ),
            run_cancellation_event=asyncio.Event(), checkpointer=checkpointer,
        )
    assert actual == completion
    assert tail_reached.is_set()
    repository.append_events.assert_awaited_once()
    assert repository.append_events.await_args.args[0][0][0].message.content[0].text == "Published before stream exit"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"status": "cancelled", "summary": "User requested cancellation"},
                                       {"status": "passed", "summary": ""}])
async def test_finish_tool_rejects_invalid_status_or_empty_summary(task_runtime: _TaskRuntime, arguments: dict[str, str]) -> None:
    state = initial_tool_state()
    state["messages"] = [AIMessage(id="finish-request", content="Finish based on observations", tool_calls=[{
        "name": "finish_task", "args": arguments, "id": "finish", "type": "tool_call",
    }])]
    update = await task_runtime.validate_message(state)
    assert update["route"] == "cycle_guard"
    task_runtime.event_recorder.record_message_validation_failed.assert_awaited_once()  # type: ignore[attr-defined]


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
        artifacts=task_runtime.runtime_context.artifacts, model_provider=ModelProvider({model.model_id: model}),
        prompt_catalog=PromptCatalog(),
        model_response_max_attempts=1, image_history_rounds=1,
    )
    graph, initial, runtime_context = await factory.build(
        task_input=TaskAgentInput(
            task_context=task_runtime.runtime_context.task_context,
            tools=(StructuredTool.from_function(coroutine=observe, description="Observe target"), *TASK_AGENT_TOOLS),
            resource_catalog=ResourceCatalogSnapshot(),
            model_profile_id=model.profile_snapshot.profile_id,
        ),
        run_cancellation_event=task_runtime.run_cancellation_event, checkpointer=None,
        event_recorder=event_recorder,
    )
    with tracing_context(enabled=False):
        final_state = await graph.ainvoke(initial, context=runtime_context)
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
        artifacts=task_runtime.runtime_context.artifacts, model_provider=ModelProvider({model.model_id: model}),
        prompt_catalog=PromptCatalog(),
        tool_call_timeout_seconds=0.05, image_history_rounds=1,
    )
    graph, initial, runtime_context = await factory.build(
        task_input=TaskAgentInput(
            task_context=task_runtime.runtime_context.task_context,
            tools=(StructuredTool.from_function(coroutine=observe, description="Observe target"), *TASK_AGENT_TOOLS),
            resource_catalog=ResourceCatalogSnapshot(),
            model_profile_id=model.profile_snapshot.profile_id,
        ),
        run_cancellation_event=task_runtime.run_cancellation_event, checkpointer=None,
        event_recorder=task_runtime.event_recorder,
    )
    try:
        with tracing_context(enabled=False):
            final_state = await asyncio.wait_for(graph.ainvoke(initial, context=runtime_context), 5)
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
@pytest.mark.parametrize("status", ["success", "error"])
async def test_each_image_discloses_its_id_and_keeps_its_public_reference(
    task_runtime: _TaskRuntime, status: str,
) -> None:
    artifacts = [Artifact(
        id=str(uuid4()), test_run_id=None, task_run_id=task_runtime.runtime_context.task_context.task_run_id,
        type=ArtifactType.IMAGE, mime_type="image/png", size_bytes=len(base64.b64decode(PNG_BASE64)),
        sha256="a" * 64, created_at=datetime.now(timezone.utc),
    ) for _ in range(2)]
    task_runtime.runtime_context.artifacts.save_image.side_effect = artifacts  # type: ignore[attr-defined]
    message = ToolMessage(id="images", tool_call_id="call", status=status, content=[
        {"type": "text", "text": "before"},
        {"type": "image", "base64": PNG_BASE64, "mime_type": "image/png", "id": "external-id"},
        {"type": "text", "text": "between"},
        {"type": "image", "base64": PNG_BASE64, "mime_type": "image/png"},
        {"type": "text", "text": "after"},
    ])
    prepared = await task_runtime._prepare_tool_result(message)
    blocks = prepared.content_blocks
    assert [block["type"] for block in blocks] == ["text", "text", "image", "text", "text", "image", "text"]
    assert [blocks[index].get("text") for index in (0, 1, 3, 4, 6)] == [
        "before", f"[Image artifact {artifacts[0].id}]", "between", f"[Image artifact {artifacts[1].id}]", "after",
    ]
    assert [blocks[index].get("id") for index in (2, 5)] == [artifact.id for artifact in artifacts]
    assert all("extras" not in blocks[index] for index in (1, 4))
    assert message.content_blocks[1].get("id") == "external-id"
    newest = ToolMessage(id="newest", tool_call_id="latest", content=[
        {"type": "image", "base64": PNG_BASE64, "mime_type": "image/png", "id": str(uuid4())},
    ])
    expired = task_runtime.model_image_window.collect_expired_updates([
        AIMessage(content="first"), prepared, AIMessage(content="latest"), newest,
    ])[0]
    assert artifacts[0].id in expired.text and artifacts[1].id in expired.text
    assert project_run_message(expired) == project_run_message(prepared)


@pytest.mark.asyncio
async def test_all_image_processing_failures_are_reported(task_runtime: _TaskRuntime) -> None:
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
