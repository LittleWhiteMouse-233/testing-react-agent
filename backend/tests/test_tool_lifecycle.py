"""Controlled graph tests for cancellation ownership and exception propagation."""
from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphBubbleUp
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from langsmith import tracing_context

from app.artifacts import ArtifactStore
from app.domain import planning
from app.domain.execution import TaskAgentCompletion
from app.domain.errors import TaskAgentCancelled, ToolCallError, ToolCleanupError, describe_exception
from app.execution.task_agent import TaskAgentGraphState, _TaskRuntime
from app.llm import ScriptedChatModelClient
from app.prompts import load_prompt


@pytest.fixture
def task_runtime() -> _TaskRuntime:
    return _TaskRuntime(
        artifacts=create_autospec(ArtifactStore, instance=True),
        run_cancellation_event=asyncio.Event(), task_run_id=str(uuid4()),
        task=planning.TestTask(test_task_id=str(uuid4()), definition=planning.TestTaskDefinition(
            type=planning.TestTaskType.ACT, title="Observe", goal="Observe the target",
            success_criteria=["Expected content is visible"], max_cycles=3,
        )),
        previous_task_runs=[], tools=(), model_client=ScriptedChatModelClient(),
        prompt_definition=load_prompt("act"), model_response_max_attempts=3,
        screenshot_history_rounds=1, tool_call_timeout_seconds=5,
    )


def initial_tool_state() -> TaskAgentGraphState:
    return {
        "messages": [AIMessage(content="", tool_calls=[{"name": "observe", "args": {}, "id": "call"}])],
        "cycle_count": 1, "model_attempt": 1, "completion": None, "route": "tools",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["timeout", "cancel", "overlap"])
@pytest.mark.parametrize("failure_kind", ["none", "exception", "group"])
async def test_cleanup_settles_before_after_tools(
    task_runtime: _TaskRuntime, trigger: str, failure_kind: str,
) -> None:
    started, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    cleanup_failure = (
        ExceptionGroup("session cleanup", [RuntimeError("cleanup failed")])
        if failure_kind == "group" else RuntimeError("cleanup failed")
    )
    cancellation_count = 0
    calls = 0
    invocations: list[asyncio.Task[Any]] = []
    task_runtime.tool_call_timeout_seconds = 5 if trigger == "cancel" else 0.2

    async def observe() -> str:
        nonlocal cancellation_count, calls
        calls += 1
        current = asyncio.current_task()
        assert current is not None
        invocations.append(current)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release_cleanup.wait()
            invocation = asyncio.current_task()
            assert invocation is not None
            cancellation_count = invocation.cancelling()
            if failure_kind != "none":
                raise cleanup_failure
            # Even an operation that returns after cancellation must not supply
            # a late result to State or evidence processing.
            return "LATE RESULT MUST NOT BE PUBLISHED"
        raise AssertionError("operation cannot finish before cancellation")

    graph = StateGraph(TaskAgentGraphState)
    graph.add_node("tools", ToolNode(
        [StructuredTool.from_function(coroutine=observe, description="Observe target")],
        awrap_tool_call=task_runtime.execute_tool_call,
    ))
    graph.add_node("after_tools", task_runtime.after_tools)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", "after_tools")
    graph.add_edge("after_tools", END)

    compiled = graph.compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "lifecycle"}}

    async def consume_live_stream():
        async for _ in compiled.astream(initial_tool_state(), config=config,
                                        stream_mode=["updates", "custom"], durability="exit"):
            pass
        return (await compiled.aget_state(config)).values

    with tracing_context(enabled=False):
        execution = asyncio.create_task(consume_live_stream())
        await asyncio.wait_for(started.wait(), 5)
        if trigger == "cancel":
            task_runtime.run_cancellation_event.set()
        await asyncio.wait_for(cleaning.wait(), 5)
        if trigger == "overlap":
            task_runtime.run_cancellation_event.set()
        assert not execution.done()
        assert invocations and not invocations[0].done()
        release_cleanup.set()
        if failure_kind != "none":
            with pytest.raises(ToolCleanupError) as captured:
                await asyncio.wait_for(execution, 5)
            assert captured.value.__cause__ is cleanup_failure
            assert all(invocation.done() for invocation in invocations)
            return
        if trigger in {"cancel", "overlap"}:
            with pytest.raises(TaskAgentCancelled):
                await asyncio.wait_for(execution, 5)
            assert calls == 1 and cancellation_count == 1
            assert all(invocation.done() for invocation in invocations)
            return
        final_state = await asyncio.wait_for(execution, 5)

    assert calls == 1
    assert cancellation_count == 1
    assert all(invocation.done() for invocation in invocations)
    returned = final_state["messages"][-1]
    assert isinstance(returned, ToolMessage)
    assert returned.tool_call_id == "call" and returned.status == "error"
    assert "LATE RESULT" not in returned.text
    assert final_state["completion"] is None  # a clean timeout remains model-correctable


@pytest.mark.asyncio
@pytest.mark.parametrize("control_error", [GraphBubbleUp(), asyncio.CancelledError(),
    ExceptionGroup("controls", [GraphBubbleUp(), RuntimeError("other")])])
async def test_control_exceptions_are_not_tool_failures(
    task_runtime: _TaskRuntime, control_error: BaseException,
) -> None:
    async def execute(request: ToolCallRequest):
        raise control_error

    with pytest.raises(type(control_error)) as captured:
        await task_runtime.execute_tool_call(tool_request(), execute)
    assert captured.value is control_error


@pytest.mark.asyncio
@pytest.mark.parametrize("external_cancel", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
async def test_product_cancellation_does_not_swallow_cleanup_control(
    task_runtime: _TaskRuntime, external_cancel: bool, grouped: bool,
) -> None:
    started = asyncio.Event()
    failures: list[BaseException] = []
    task_runtime.on_cleanup_failure = failures.append
    cleanup_failure = (
        ExceptionGroup("cleanup", [GraphBubbleUp("control during cleanup"), OSError("close failed")])
        if grouped else GraphBubbleUp("control during cleanup")
    )

    async def execute(request: ToolCallRequest):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup_failure

    wrapper = asyncio.create_task(task_runtime.execute_tool_call(tool_request(), execute))
    await started.wait()
    if external_cancel:
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await wrapper
        assert captured.value.__cause__ is cleanup_failure
    else:
        task_runtime.run_cancellation_event.set()
        with pytest.raises(type(cleanup_failure)) as captured:
            await wrapper
        assert captured.value is cleanup_failure
    assert failures == [cleanup_failure]


@pytest.mark.asyncio
async def test_tool_timeout_error_is_fatal(task_runtime: _TaskRuntime) -> None:
    async def execute(request: ToolCallRequest):
        raise TimeoutError("tool transport timed out")

    with pytest.raises(TimeoutError, match="tool transport timed out"):
        await task_runtime.execute_tool_call(tool_request(), execute)


def tool_request() -> ToolCallRequest:
    return ToolCallRequest(tool_call={"name": "observe", "args": {}, "id": "call"},
                           tool=None, state=initial_tool_state(), runtime=cast(Any, None))


@pytest.mark.asyncio
async def test_application_error_propagates_without_completion(task_runtime: _TaskRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = OSError("tool failed")

    async def execute(request: ToolCallRequest):
        raise failure

    def fail_report(*args, **kwargs):
        raise AssertionError("the tool wrapper must not construct a completion")

    monkeypatch.setattr(task_runtime, "_completion", fail_report)
    with pytest.raises(OSError) as captured:
        await task_runtime.execute_tool_call(tool_request(), execute)
    assert captured.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_repeated_external_cancellation_joins_cleanup(task_runtime: _TaskRuntime, cleanup_fails: bool) -> None:
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    invocation_tasks: list[asyncio.Task[Any]] = []
    cleanup_failure = RuntimeError("late cleanup failure")

    async def execute(request: ToolCallRequest):
        current = asyncio.current_task()
        assert current is not None
        invocation_tasks.append(current)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            if cleanup_fails:
                raise cleanup_failure
        raise AssertionError("cannot finish normally")

    wrapper = asyncio.create_task(task_runtime.execute_tool_call(tool_request(), execute))
    await started.wait()
    wrapper.cancel()
    await cleaning.wait()
    wrapper.cancel()
    await asyncio.sleep(0)
    assert not wrapper.done() and not invocation_tasks[0].done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as captured:
        await wrapper
    assert invocation_tasks[0].done() and invocation_tasks[0].cancelling() == 1
    if cleanup_fails:
        assert str(cleanup_failure) in describe_exception(captured.value, phase="Cancelled")


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
async def test_external_wrapper_cancellation_waits_for_cleanup(task_runtime: _TaskRuntime) -> None:
    started, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    cleanup_failure = RuntimeError("cleanup failed")

    async def observe() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            raise cleanup_failure

    node = ToolNode(
        [StructuredTool.from_function(coroutine=observe, description="Observe target")],
        awrap_tool_call=task_runtime.execute_tool_call,
    )
    graph = StateGraph(TaskAgentGraphState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    with tracing_context(enabled=False):
        execution = asyncio.create_task(graph.compile().ainvoke(initial_tool_state()))
        await asyncio.wait_for(started.wait(), 5)
        execution.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not execution.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await execution
    assert isinstance(captured.value.__cause__, ToolCleanupError)
    assert captured.value.__cause__.__cause__ is cleanup_failure


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


@pytest.mark.asyncio
async def test_cleanup_deadline_survives_repeated_cancellation(task_runtime: _TaskRuntime) -> None:
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    failures: list[BaseException] = []
    task_runtime.tool_cleanup_timeout_seconds = 0.08
    task_runtime.on_cleanup_failure = failures.append
    invocations: list[asyncio.Task[Any]] = []

    async def execute(request: ToolCallRequest):
        current = asyncio.current_task()
        assert current is not None
        invocations.append(current)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

        raise AssertionError("cannot finish without interruption")

    wrapper = asyncio.create_task(task_runtime.execute_tool_call(tool_request(), execute))
    await started.wait()
    wrapper.cancel()
    await cleaning.wait()
    for _ in range(3):
        wrapper.cancel()
        await asyncio.sleep(0.01)
    with pytest.raises(asyncio.CancelledError) as captured:
        await asyncio.wait_for(wrapper, 1)
    assert isinstance(captured.value.__cause__, ToolCleanupError)
    assert len(failures) == 1
    assert not invocations[0].done() and invocations[0].cancelling() == 1
    release.set()
    await asyncio.gather(*invocations, return_exceptions=True)
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_result_failure_wins_when_cancellation_is_already_set(task_runtime: _TaskRuntime) -> None:
    failure = OSError("transport failed")

    async def execute(request: ToolCallRequest):
        task_runtime.run_cancellation_event.set()
        raise failure

    with pytest.raises(OSError) as captured:
        await task_runtime.execute_tool_call(tool_request(), execute)
    assert captured.value is failure
