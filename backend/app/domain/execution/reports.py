"""Execution report facts shared by snapshot queries and exports."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict

from app.domain.execution.artifacts import Artifact
from app.domain.execution.events import NodeExecutionEvent, StoredRunEvent, ToolCallEvent
from app.domain.execution.runs import TaskRun, TestRunDetail


def group_events_by_task_run(events: list[StoredRunEvent]) -> dict[str | None, list[StoredRunEvent]]:
    """Group the timeline by TaskRun, preserving sequence within each group."""
    events_by_task_run_id: dict[str | None, list[StoredRunEvent]] = defaultdict(list)
    for stored_event in events:
        events_by_task_run_id[stored_event.event.task_run_id].append(stored_event)
    return events_by_task_run_id


class ToolCallCounts(BaseModel):
    """Calls and successful results for one named tool within one TaskRun."""
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    calls: int = 0
    successes: int = 0


class NodeTiming(BaseModel):
    """Cumulative attempt time for one node and its share of elapsed task time."""

    elapsed_seconds: float
    task_percent: float | None


class TaskRunStatistics(BaseModel):
    """Observed tool counts and node timings for one actual task execution."""

    tools: dict[str, ToolCallCounts]
    nodes: dict[str, NodeTiming]


def task_run_statistics(task_runs: list[TaskRun], events: list[StoredRunEvent]) -> dict[str, TaskRunStatistics]:
    """Aggregate each finished task execution over its complete event history."""
    grouped = group_events_by_task_run(events)
    statistics: dict[str, TaskRunStatistics] = {}
    for task in task_runs:
        if task.started_at is None or task.finished_at is None:
            continue
        task_seconds = (task.finished_at - task.started_at).total_seconds()
        tools: dict[str, ToolCallCounts] = {}
        seconds_by_node: dict[str, float] = defaultdict(float)
        for stored in grouped.get(task.id, []):
            event = stored.event
            if isinstance(event, ToolCallEvent):
                count = tools.setdefault(event.tool_name, ToolCallCounts())
                if event.type == "tool.started":
                    count.calls += 1
                else:
                    count.successes += 1
            elif isinstance(event, NodeExecutionEvent):
                seconds_by_node[event.node_name] += (event.finished_at - event.started_at).total_seconds()
        statistics[task.id] = TaskRunStatistics(tools=tools, nodes={
            name: NodeTiming(elapsed_seconds=seconds, task_percent=100 * seconds / task_seconds if task_seconds > 0 else None)
            for name, seconds in seconds_by_node.items()
        })
    return statistics


class TestRunReport(BaseModel):
    """报告事实组合；canonical detail 中的事实不会在顶层复制。

    在线读取的 events 为请求游标之后的历史；默认读取及导出包含完整历史。
    detail 与 artifacts 始终完整，并与返回的 events 来自同一读取快照。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: TestRunDetail
    events: list[StoredRunEvent]
    task_statistics: dict[str, TaskRunStatistics]
    artifacts: list[Artifact]
    # Current TestCase state, joined at read time; never part of the run environment snapshot.
    test_case_archived: bool
