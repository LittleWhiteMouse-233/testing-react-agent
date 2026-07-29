from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.models import (
    ActionDecision,
    PlanOutput,
    Task,
    TaskType,
    WaitAction,
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


def test_wait_action_range_is_enforced() -> None:
    with pytest.raises(ValidationError):
        WaitAction(type="WAIT", duration_ms=99)
    decision = ActionDecision(
        type="action",
        summary="Wait for rendering",
        action={"type": "WAIT", "duration_ms": 100},
    )
    assert decision.action.duration_ms == 100

