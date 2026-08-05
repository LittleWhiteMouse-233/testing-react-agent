from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import LLMProfileSettings, Settings
from app.domain.errors import ReasonCode
from app.domain.execution import ModelSnapshot, TaskOutcome, TaskStatus
from app.domain.planning import PlanOutput, Task, TaskType
from app.graph.task_agent import (
    ACT_POLICY,
    FinishTaskArgs,
    JUDGE_POLICY,
    policy_for,
)
from app.llm.contracts import ModelActivity
from app.llm.registry import ModelRegistry
from app.llm.test_fake import ScriptedChatModelProvider


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
    terminal = FinishTaskArgs(
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
        FinishTaskArgs(status="unknown", summary="invalid")
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
    assert ACT_POLICY.policy_id == "act"
    assert JUDGE_POLICY.policy_id == "judge"


def test_model_snapshot_has_explicit_audit_fields() -> None:
    snapshot = ScriptedChatModelProvider(model_id="audited").model_snapshot

    assert snapshot == ModelSnapshot(
        profile_id="audited",
        provider="scripted",
        model="deterministic",
        base_url=None,
        temperature=0,
        timeout_seconds=60,
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="json"),
                "info": {"id": "duplicated"},
            }
        )


def test_model_registry_defaults_to_first_profile_and_rejects_unknown_routes() -> None:
    first = ScriptedChatModelProvider(model_id="first")
    second = ScriptedChatModelProvider(model_id="second")
    registry = ModelRegistry({"first": first, "second": second})

    assert registry.model_id_for(ModelActivity.PLANNING) == "first"
    assert registry.model_id_for(ModelActivity.ACT) == "first"
    assert registry.model_id_for(ModelActivity.JUDGE) == "first"

    with pytest.raises(ValidationError, match="unknown model"):
        Settings(
            llm_profiles=[LLMProfileSettings(id="first")],
            act_model_id="missing",
        )
