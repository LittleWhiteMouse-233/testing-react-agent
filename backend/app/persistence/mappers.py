from __future__ import annotations

from datetime import datetime, timezone

from app.domain.artifacts import Artifact
from app.domain.execution import TaskRun, TestRun
from app.domain.planning import (
    TestPlan,
    TestPlanContent,
    TestTask,
    TestTaskDefinition,
)
from app.domain.test_cases import TestCase, TestCaseContent
from app.persistence.adapters import (
    load_planning_context,
    load_string_list,
    load_task_run_result,
)
from app.persistence.models import (
    ArtifactRow,
    TaskRunRow,
    TestCaseRow,
    TestPlanRow,
    TestRunRow,
    TestTaskRow,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_required(value: datetime) -> datetime:
    converted = _utc(value)
    assert converted is not None
    return converted


def test_case_from_row(row: TestCaseRow) -> TestCase:
    return TestCase(
        id=row.id,
        content=TestCaseContent(name=row.name, source_text=row.source_text),
        created_at=_utc_required(row.created_at),
    )


def test_task_from_row(row: TestTaskRow) -> TestTask:
    return TestTask(
        test_task_id=row.id,
        definition=TestTaskDefinition(
            type=row.type,
            title=row.title,
            goal=row.goal,
            success_criteria=load_string_list(row.success_criteria_json),
            max_cycles=row.max_cycles,
        ),
    )


def test_plan_from_rows(row: TestPlanRow, tasks: list[TestTaskRow]) -> TestPlan:
    return TestPlan(
        id=row.id,
        test_case_id=row.test_case_id,
        version_number=row.version_number,
        origin=row.origin,
        derived_from_plan_id=row.derived_from_plan_id,
        planning_context=load_planning_context(row.planning_context_json),
        content=TestPlanContent[TestTask](
            title=row.title,
            setup_steps=load_string_list(row.setup_steps_json),
            assumptions=load_string_list(row.assumptions_json),
            tasks=[test_task_from_row(task) for task in tasks],
        ),
        created_at=_utc_required(row.created_at),
    )


def test_run_from_row(row: TestRunRow) -> TestRun:
    return TestRun(
        id=row.id,
        test_plan_id=row.test_plan_id,
        device_id=row.device_id,
        status=row.status,
        verdict=row.verdict,
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
        created_at=_utc_required(row.created_at),
    )


def task_run_from_row(row: TaskRunRow) -> TaskRun:
    return TaskRun(
        id=row.id,
        test_run_id=row.test_run_id,
        test_task_id=row.test_task_id,
        status=row.status,
        cycle_count=row.cycle_count,
        result=(
            load_task_run_result(row.result_json)
            if row.result_json is not None
            else None
        ),
        started_at=_utc(row.started_at),
        finished_at=_utc(row.finished_at),
    )


def artifact_from_row(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        test_run_id=row.test_run_id,
        task_run_id=row.task_run_id,
        type=row.type,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        created_at=_utc_required(row.created_at),
    )
