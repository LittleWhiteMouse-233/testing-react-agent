from __future__ import annotations

from typing import Any, cast

from pydantic import TypeAdapter

from app.domain.execution import RUN_EVENT_ADAPTER, RunEvent
from app.domain.execution import TaskRunResult, TestRunSnapshot
from app.domain.planning import TestPlanPlanningContext


PLANNING_CONTEXT_ADAPTER = TypeAdapter(TestPlanPlanningContext)
TEST_RUN_SNAPSHOT_ADAPTER = TypeAdapter(TestRunSnapshot)
TASK_RUN_RESULT_ADAPTER = TypeAdapter(TaskRunResult)
STRING_LIST_ADAPTER = TypeAdapter(list[str])


def _object_json(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("business JSON root must be an object")
    return cast(dict[str, Any], value)


def dump_planning_context(value: TestPlanPlanningContext) -> dict[str, Any]:
    return _object_json(
        PLANNING_CONTEXT_ADAPTER.dump_python(value, mode="json")
    )


def dump_test_run_snapshot(value: TestRunSnapshot) -> dict[str, Any]:
    return _object_json(TEST_RUN_SNAPSHOT_ADAPTER.dump_python(value, mode="json"))


def dump_task_run_result(value: TaskRunResult) -> dict[str, Any]:
    return _object_json(TASK_RUN_RESULT_ADAPTER.dump_python(value, mode="json"))


def dump_string_list(value: list[str]) -> list[str]:
    return cast(list[str], STRING_LIST_ADAPTER.dump_python(value, mode="json"))


def load_string_list(value: object) -> list[str]:
    return STRING_LIST_ADAPTER.validate_python(value)


def load_planning_context(value: object) -> TestPlanPlanningContext:
    return PLANNING_CONTEXT_ADAPTER.validate_python(value)


def load_test_run_snapshot(value: object) -> TestRunSnapshot:
    return TEST_RUN_SNAPSHOT_ADAPTER.validate_python(value)


def load_task_run_result(value: object) -> TaskRunResult:
    return TASK_RUN_RESULT_ADAPTER.validate_python(value)


def dump_run_event(event: RunEvent) -> tuple[str, dict[str, Any]]:
    value = RUN_EVENT_ADAPTER.dump_python(event, mode="json")
    event_type = value.pop("type")
    value.pop("test_run_id")
    value.pop("task_run_id")
    return str(event_type), value


def load_run_event(
    *,
    event_type: str,
    test_run_id: str,
    task_run_id: str | None,
    payload: object,
) -> RunEvent:
    if not isinstance(payload, dict):
        raise TypeError("event payload_json must be an object")
    reserved = {"type", "test_run_id", "task_run_id"} & payload.keys()
    if reserved:
        raise ValueError(
            "event payload_json contains reserved columns: "
            + ", ".join(sorted(reserved))
        )
    return RUN_EVENT_ADAPTER.validate_python(
        {
            "type": event_type,
            "test_run_id": test_run_id,
            "task_run_id": task_run_id,
            **payload,
        }
    )
