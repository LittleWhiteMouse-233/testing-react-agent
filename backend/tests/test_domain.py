from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.domain.errors import ReasonCode
from app.domain.execution import TaskOutcome, TaskStatus, TaskTerminalDecision
from app.domain.planning import PlanOutput, Task, TaskType
from app.graph.task_agent import (
    ACT_POLICY,
    JUDGE_POLICY,
    _trim_history_preserving_tool_pairs,
    policy_for,
)


def test_plan_requires_unique_task_ids_and_observable_criteria() -> None:
    task = Task(
        task_id="task-1",
        type=TaskType.ACT,
        title="Open settings",
        goal="Settings is visible",
        success_criteria=["Settings title is visible"],
    )
    with pytest.raises(ValidationError):
        PlanOutput(title="duplicate", tasks=[task, task])
    with pytest.raises(ValidationError):
        Task(
            task_id="task-2",
            type=TaskType.JUDGE,
            title="Judge",
            goal="Judge value",
            success_criteria=[""],
        )


def test_terminal_and_outcome_are_strict_domain_contracts() -> None:
    terminal = TaskTerminalDecision(
        status="passed",
        summary="The latest screenshot meets the criterion",
    )
    outcome = TaskOutcome(
        status=TaskStatus.PASSED,
        reason_code=ReasonCode.COMPLETED,
        summary=terminal.summary,
        cycle_count=1,
        evidence_artifact_ids=["artifact-1"],
    )
    assert outcome.evidence_artifact_ids == ["artifact-1"]
    with pytest.raises(ValidationError):
        TaskTerminalDecision(status="unknown", summary="invalid")
    with pytest.raises(ValidationError):
        TaskOutcome(
            status=TaskStatus.FAILED,
            reason_code=ReasonCode.ASSERTION_FAILED,
            summary="Missing required screenshot evidence",
            cycle_count=1,
        )


def test_task_type_selects_policy_without_changing_graph_contract() -> None:
    act = Task(
        task_id="act",
        type=TaskType.ACT,
        title="Act",
        goal="Navigate",
        success_criteria=["Target is visible"],
    )
    judge = act.model_copy(update={"task_id": "judge", "type": TaskType.JUDGE})
    assert policy_for(act) is ACT_POLICY
    assert policy_for(judge) is JUDGE_POLICY
    assert ACT_POLICY.allow_device_keys is True
    assert JUDGE_POLICY.allow_device_keys is False
    assert JUDGE_POLICY.allow_mutating_external_tools is False


def test_history_trimming_never_splits_tool_call_and_tool_message() -> None:
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "device_wait",
                "args": {"duration_ms": 100},
                "id": "paired-call",
                "type": "tool_call",
            }
        ],
    )
    result = ToolMessage(
        content='{"status":"succeeded"}',
        tool_call_id="paired-call",
    )
    history = [
        HumanMessage(content="old " * 500),
        call,
        result,
        HumanMessage(content="latest observation"),
    ]

    trimmed = _trim_history_preserving_tool_pairs(history, max_tokens=10)

    assert trimmed[-1].content == "latest observation"
    assert (call in trimmed) is (result in trimmed)
