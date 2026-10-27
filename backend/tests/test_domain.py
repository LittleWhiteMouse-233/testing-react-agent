from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import ValidationError

from app.config import LLMProfileSettings
from app.domain.activity import AgentActivity
from app.domain.execution import (
    Artifact,
    ArtifactType,
    ReasonCode,
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
    TestRunVerdict as RunVerdict,
    aggregate_test_run_verdict,
)
from app.domain.planning import (
    TestPlanContent as PlanContent,
    TestTask as PlanTask,
    TestTaskDefinition as TaskDefinition,
    TestTaskType as TaskType,
    identify_plan_content,
)
from app.llm import ModelProvider, ScriptedChatModelClient, profile_snapshot_from_settings


RUN_ID = "11111111-1111-4111-8111-111111111111"
TASK_RUN_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "33333333-3333-4333-8333-333333333333"
ARTIFACT_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def definition(task_type: TaskType = TaskType.JUDGE, task_no: int = 1) -> TaskDefinition:
    return TaskDefinition(
        task_no=task_no,
        type=task_type,
        title="Check",
        goal="Check the visible state",
        success_criteria=["Title is visible"],
    )


def terminal_task(status: TaskRunStatus) -> TaskRun:
    return TaskRun(
        id=TASK_RUN_ID,
        test_run_id=RUN_ID,
        test_task_id=TASK_ID,
        sequence=1,
        status=status,
        cycle_count=1,
        result=TaskRunResult(
            reason_code=ReasonCode.COMPLETED,
            summary="done",
        ),
        started_at=NOW,
        finished_at=NOW,
    )


def test_generic_plan_content_has_one_task_shape_and_fresh_identity() -> None:
    draft = PlanContent[TaskDefinition](
        title="Plan",
        setup_steps=[],
        assumptions=[],
        tasks=[definition(TaskType.ACT), definition(task_no=2)],
    )
    first = identify_plan_content(draft)
    second = identify_plan_content(draft)

    assert all(isinstance(task, PlanTask) for task in first.tasks)
    assert {task.test_task_id for task in first.tasks}.isdisjoint(
        {task.test_task_id for task in second.tasks}
    )
    with pytest.raises(ValidationError):
        PlanContent[TaskDefinition](
            title="Plan",
            setup_steps=[],
            assumptions=[],
            tasks=[definition().model_copy(update={"success_criteria": [""]})],
        )


def test_task_result_and_deterministic_verdict_do_not_copy_status_or_cycle() -> None:
    result_fields = set(TaskRunResult.model_fields)
    assert result_fields == {"reason_code", "summary", "memory"}
    tasks = [PlanTask(test_task_id=TASK_ID, definition=definition())]
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.PASSED)], tasks) == RunVerdict.PASS
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.FAILED)], tasks) == RunVerdict.FAIL
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.BLOCKED)], tasks) == RunVerdict.BLOCKED
    with pytest.raises(ValueError, match="explicit cancellation path"):
        aggregate_test_run_verdict(
            [
                terminal_task(TaskRunStatus.BLOCKED).model_copy(
                    update={
                        "status": TaskRunStatus.CANCELLED,
                        "result": TaskRunResult(
                            reason_code=ReasonCode.USER_CANCELLED,
                            summary="cancelled",
                        ),
                    }
                )
            ], tasks,
        )


@pytest.mark.parametrize("numbers", [[1, 1], [2, 1], [1, 3]])
def test_plan_rejects_invalid_task_numbers_without_renumbering(numbers: list[int]) -> None:
    with pytest.raises(ValidationError, match="task_no"):
        PlanContent[TaskDefinition](title="Plan", setup_steps=[], assumptions=[],
                                    tasks=[definition(task_no=number) for number in numbers])


@pytest.mark.parametrize("task_type,target", [("goal", None), ("goal", 0), ("goal", 2),
                                               ("goal", 3), ("act", 1), ("judge", 1)])
def test_task_rejects_invalid_goal_jump(task_type: str, target: int | None) -> None:
    with pytest.raises(ValidationError):
        TaskDefinition.model_validate({"task_no": 2, "type": task_type, "title": "Loop",
            "goal": "All nodes checked", "success_criteria": ["No pending nodes"],
            "jump_back_to_task_no": target})


def test_goal_failures_are_control_results_but_blocking_takes_precedence() -> None:
    goal = TaskDefinition.model_validate({"task_no": 2, "type": "goal", "title": "Loop",
        "goal": "All nodes checked", "success_criteria": ["No pending nodes"], "jump_back_to_task_no": 1})
    goal_id = "55555555-5555-4555-8555-555555555555"
    tasks = [PlanTask(test_task_id=TASK_ID, definition=definition()),
             PlanTask(test_task_id=goal_id, definition=goal)]
    failed_goal = terminal_task(TaskRunStatus.FAILED).model_copy(update={"test_task_id": goal_id, "sequence": 2})
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.PASSED), failed_goal], tasks) == RunVerdict.PASS
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.FAILED), failed_goal,
                                      terminal_task(TaskRunStatus.BLOCKED)], tasks) == RunVerdict.BLOCKED
    exhausted = failed_goal.model_copy(update={"result": TaskRunResult(
        reason_code=ReasonCode.GOAL_FAILURE_LIMIT, summary="Still pending")})
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.FAILED), exhausted], tasks) == RunVerdict.BLOCKED


@pytest.mark.parametrize("memory,expected", [
    (None, None), ("", None), (" \t\r\n", None),
    (" A: model TV-X1\nfirmware V2.3.7 ", " A: model TV-X1\nfirmware V2.3.7 "),
])
def test_task_memory_normalizes_only_blank_content(memory: str | None, expected: str | None) -> None:
    result = TaskRunResult(reason_code=ReasonCode.COMPLETED, summary="Observed A", memory=memory)
    assert result.memory == expected
    assert TaskRunResult(reason_code=ReasonCode.COMPLETED, summary="No memory").memory is None


@pytest.mark.asyncio
async def test_scripted_terminal_turn_preserves_memory() -> None:
    client = ScriptedChatModelClient(turns=[{"status": "passed", "summary": "Observed A", "memory": "A: V2.3.7"}])
    reply = await client.create_model().bind_tools([]).ainvoke([HumanMessage(content="Read A")])
    assert reply.tool_calls[0]["args"]["memory"] == "A: V2.3.7"


@pytest.mark.asyncio
async def test_scripted_turn_callback_receives_actual_messages() -> None:
    def remember(messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content=messages[-1].text)

    client = ScriptedChatModelClient(turns=[remember])
    messages = [HumanMessage(content="Actual image artifact ID")]
    reply = await client.create_model().bind_tools([]).ainvoke(messages)
    assert reply.content == messages[0].content
    assert client.invocations == [messages]


def test_artifact_owner_is_direct_and_exclusive() -> None:
    image = Artifact(
        id=ARTIFACT_ID,
        test_run_id=None,
        task_run_id=TASK_RUN_ID,
        type=ArtifactType.IMAGE,
        mime_type="image/png",
        size_bytes=3,
        sha256="a" * 64,
        created_at=NOW,
    )
    assert image.task_run_id == TASK_RUN_ID
    with pytest.raises(ValidationError, match="exactly one"):
        Artifact(
            id=ARTIFACT_ID,
            test_run_id=RUN_ID,
            task_run_id=TASK_RUN_ID,
            type=ArtifactType.IMAGE,
            mime_type="image/png",
            size_bytes=3,
            sha256="a" * 64,
            created_at=NOW,
        )


def test_settings_to_snapshot_is_a_single_secret_free_projection() -> None:
    settings = LLMProfileSettings(
        id="audited",
        mode="real",
        base_url="https://example.invalid/v1",
        api_key="secret",
        model="vision",
    )
    snapshot = profile_snapshot_from_settings(settings)
    assert snapshot.profile_id == "audited"
    assert "api_key" not in snapshot.model_dump()


def test_activity_conversion_and_model_provider_routes_are_explicit() -> None:
    first = ScriptedChatModelClient(model_id="first")
    second = ScriptedChatModelClient(model_id="second")
    provider = ModelProvider(
        {"first": first, "second": second},
        planning_model_id="second",
        execution_model_id="first",
    )
    assert provider.client_for_activity(AgentActivity.PLANNING) is second
    assert provider.client_for_activity(AgentActivity.EXECUTION) is first
    assert provider.client_for_activity(AgentActivity.EXECUTION) is first
    assert provider.client_by_id("second") is second


def test_model_provider_uses_ordered_default_and_one_profile_for_two_routes() -> None:
    first = ScriptedChatModelClient(model_id="first")
    second = ScriptedChatModelClient(model_id="second")
    provider = ModelProvider(
        {"first": first, "second": second},
        execution_model_id="second",
    )
    assert provider.client_for_activity(AgentActivity.PLANNING) is first
    assert provider.client_for_activity(AgentActivity.EXECUTION) is second
    assert provider.client_for_activity(AgentActivity.EXECUTION) is second
