from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.config import LLMProfileSettings, Settings
from app.domain.activity import AgentActivity, activity_for_task
from app.domain.artifacts import Artifact, ArtifactType
from app.domain.errors import ReasonCode
from app.domain.execution import (
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
from app.llm.registry import ModelRegistry
from app.llm.snapshots import profile_snapshot_from_settings
from app.llm.test_fake import ScriptedChatModelProvider


RUN_ID = "11111111-1111-4111-8111-111111111111"
TASK_RUN_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "33333333-3333-4333-8333-333333333333"
ARTIFACT_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def definition(task_type: TaskType = TaskType.JUDGE) -> TaskDefinition:
    return TaskDefinition(
        type=task_type,
        title="Check",
        goal="Check the visible state",
        success_criteria=["Title is visible"],
        max_cycles=2,
    )


def terminal_task(status: TaskRunStatus) -> TaskRun:
    evidence = [ARTIFACT_ID] if status in {TaskRunStatus.PASSED, TaskRunStatus.FAILED} else []
    return TaskRun(
        id=TASK_RUN_ID,
        test_run_id=RUN_ID,
        test_task_id=TASK_ID,
        status=status,
        cycle_count=1,
        result=TaskRunResult(
            reason_code=ReasonCode.COMPLETED,
            summary="done",
            evidence_artifact_ids=evidence,
        ),
        started_at=NOW,
        finished_at=NOW,
    )


def test_generic_plan_content_has_one_task_shape_and_fresh_identity() -> None:
    draft = PlanContent[TaskDefinition](
        title="Plan",
        setup_steps=[],
        assumptions=[],
        tasks=[definition(TaskType.ACT), definition()],
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
    assert result_fields == {"reason_code", "summary", "evidence_artifact_ids"}
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.PASSED)]) == RunVerdict.PASS
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.FAILED)]) == RunVerdict.FAIL
    assert aggregate_test_run_verdict([terminal_task(TaskRunStatus.BLOCKED)]) == RunVerdict.BLOCKED
    with pytest.raises(ValidationError, match="screenshot evidence"):
        terminal_task(TaskRunStatus.PASSED).model_copy(
            update={
                "result": TaskRunResult(
                    reason_code=ReasonCode.COMPLETED,
                    summary="missing evidence",
                    evidence_artifact_ids=[],
                )
            }
        ).model_validate(
            terminal_task(TaskRunStatus.PASSED).model_copy(
                update={
                    "result": TaskRunResult(
                        reason_code=ReasonCode.COMPLETED,
                        summary="missing evidence",
                        evidence_artifact_ids=[],
                    )
                }
            ).model_dump()
        )


def test_artifact_owner_is_direct_and_exclusive() -> None:
    screenshot = Artifact(
        id=ARTIFACT_ID,
        test_run_id=None,
        task_run_id=TASK_RUN_ID,
        type=ArtifactType.SCREENSHOT,
        mime_type="image/png",
        size_bytes=3,
        sha256="a" * 64,
        created_at=NOW,
    )
    assert screenshot.task_run_id == TASK_RUN_ID
    with pytest.raises(ValidationError, match="exactly one"):
        Artifact(
            id=ARTIFACT_ID,
            test_run_id=RUN_ID,
            task_run_id=TASK_RUN_ID,
            type=ArtifactType.SCREENSHOT,
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
    with pytest.raises(ValidationError, match="unknown model"):
        Settings(
            llm_profiles=[LLMProfileSettings(id="first")],
            act_model_id="missing",
        )


def test_activity_conversion_and_model_registry_are_explicit() -> None:
    assert activity_for_task(TaskType.ACT) == AgentActivity.ACT
    assert activity_for_task(TaskType.JUDGE) == AgentActivity.JUDGE
    first = ScriptedChatModelProvider(model_id="first")
    registry = ModelRegistry({"first": first})
    assert registry.for_activity(AgentActivity.PLANNING).profile_snapshot.profile_id == "first"
