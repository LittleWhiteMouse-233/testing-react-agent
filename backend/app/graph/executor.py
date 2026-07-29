from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import (
    ActionDecision,
    ActionTimeout,
    BlockedDecision,
    CaptureFailed,
    DecisionContext,
    DeviceCapabilities,
    OverallResult,
    PlanOutput,
    RunStatus,
    Task,
    TaskFailDecision,
    TaskPassDecision,
    TaskStatus,
    TaskType,
    ToolAction,
    ToolContext,
    TransientToolError,
    WaitAction,
)
from app.persistence.models import ArtifactRow, TaskRunRow, TestRunRow
from app.llm.contracts import LLMProvider
from app.device.contracts import DeviceController
from app.services.artifacts import ArtifactStore
from app.services.events import EventWriter
from app.services.registry import RunRegistry
from app.services.tools import ToolProvider


def now() -> datetime:
    return datetime.now(timezone.utc)


class GraphState(TypedDict, total=False):
    run_id: str
    plan_revision_id: str
    tasks: list[dict[str, Any]]
    task_index: int
    current_task_run_id: str | None
    cycle_count: int
    latest_artifact_id: str | None
    latest_decision_summary: str | None
    recent_history: list[dict[str, Any]]
    capabilities: dict[str, Any]
    route: str
    run_blocked: bool


class AgentGraphExecutor:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        events: EventWriter,
        artifacts: ArtifactStore,
        llm: LLMProvider,
        tools: ToolProvider,
        registry: RunRegistry,
        devices: dict[str, DeviceController],
        checkpoint_path: str,
        action_timeout_seconds: float = 15,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.artifacts = artifacts
        self.llm = llm
        self.tools = tools
        self.registry = registry
        self.devices = devices
        self.checkpoint_path = checkpoint_path
        self.action_timeout_seconds = action_timeout_seconds

    async def run(self, run_id: str) -> None:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if not run:
                return
            plan = PlanOutput.model_validate(run.snapshot_json["plan"])
            initial: GraphState = {
                "run_id": run_id,
                "plan_revision_id": run.plan_revision_id,
                "tasks": [task.model_dump(mode="json") for task in plan.tasks],
                "task_index": 0,
                "current_task_run_id": None,
                "cycle_count": 0,
                "latest_artifact_id": None,
                "latest_decision_summary": None,
                "recent_history": [],
                "capabilities": {},
                "route": "prepare",
                "run_blocked": False,
            }
        try:
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as checkpointer:
                graph = self._build_graph().compile(checkpointer=checkpointer)
                # StepEvent is this application's audit trail. Disable ambient
                # LangSmith tracing so a developer's global environment cannot
                # export screenshots or run context unexpectedly.
                with tracing_context(enabled=False):
                    await graph.ainvoke(
                        initial,
                        config={"configurable": {"thread_id": run_id}},
                    )
        except Exception as exc:
            await self._unexpected_failure(run_id, exc)
        finally:
            self.registry.unregister(run_id)

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(GraphState)
        graph.add_node("prepare_run", self._prepare_run)
        graph.add_node("select_next_task", self._select_next_task)
        graph.add_node("check_cycle", self._check_cycle)
        graph.add_node("capture_observation", self._capture_observation)
        graph.add_node("decide", self._decide)
        graph.add_node("execute_action", self._execute_action)
        graph.add_node("skip_remaining", self._skip_remaining)
        graph.add_node("finalize_run", self._finalize_run)
        graph.add_edge(START, "prepare_run")
        graph.add_conditional_edges(
            "prepare_run",
            lambda state: state["route"],
            {"select": "select_next_task", "finalize": "finalize_run"},
        )
        graph.add_conditional_edges(
            "select_next_task",
            lambda state: state["route"],
            {"check": "check_cycle", "finalize": "finalize_run"},
        )
        graph.add_conditional_edges(
            "check_cycle",
            lambda state: state["route"],
            {"observe": "capture_observation", "skip": "skip_remaining"},
        )
        graph.add_conditional_edges(
            "capture_observation",
            lambda state: state["route"],
            {
                "decide": "decide",
                "skip": "skip_remaining",
                "finalize": "finalize_run",
            },
        )
        graph.add_conditional_edges(
            "decide",
            lambda state: state["route"],
            {
                "execute": "execute_action",
                "select": "select_next_task",
                "skip": "skip_remaining",
            },
        )
        graph.add_conditional_edges(
            "execute_action",
            lambda state: state["route"],
            {
                "check": "check_cycle",
                "skip": "skip_remaining",
                "finalize": "finalize_run",
            },
        )
        graph.add_edge("skip_remaining", "finalize_run")
        graph.add_edge("finalize_run", END)
        return graph

    async def _run_row(self, run_id: str) -> TestRunRow:
        async with self.sessions() as session:
            row = await session.get(TestRunRow, run_id)
            if not row:
                raise LookupError(f"Run not found: {run_id}")
            return row

    def _device_for(self, run: TestRunRow) -> DeviceController:
        device = self.devices.get(run.device_id)
        if not device:
            raise LookupError(f"Device is not registered: {run.device_id}")
        return device

    async def _prepare_run(self, state: GraphState) -> dict[str, Any]:
        run = await self._run_row(state["run_id"])
        try:
            device = self._device_for(run)
            health = await device.health()
            if not health.available:
                raise RuntimeError(health.message or "Device is unavailable")
            capabilities = await device.capabilities()
        except Exception as exc:
            await self.events.append(
                run_id=run.id,
                event_type="error",
                payload={"classification": "DeviceUnavailable", "message": str(exc)},
                dedup_key=f"{run.id}:prepare:error",
            )
            return {"route": "finalize", "run_blocked": True}
        async with self.sessions() as session:
            row = await session.get(TestRunRow, run.id)
            row.status = RunStatus.RUNNING.value
            row.started_at = now()
            await session.commit()
        await self.events.append(
            run_id=run.id,
            event_type="run_started",
            payload={"device_id": run.device_id},
            dedup_key=f"{run.id}:run_started",
        )
        return {"route": "select", "capabilities": capabilities.model_dump(mode="json")}

    async def _select_next_task(self, state: GraphState) -> dict[str, Any]:
        index = state["task_index"]
        tasks = state["tasks"]
        if index >= len(tasks):
            return {"route": "finalize", "current_task_run_id": None}
        task = Task.model_validate(tasks[index])
        task_run_id = str(uuid4())
        async with self.sessions() as session:
            existing = await session.scalar(
                select(TaskRunRow).where(
                    TaskRunRow.test_run_id == state["run_id"],
                    TaskRunRow.task_index == index,
                )
            )
            if existing:
                task_run_id = existing.id
                existing.status = TaskStatus.RUNNING.value
                existing.started_at = existing.started_at or now()
                existing.cycle_count = 0
            else:
                session.add(
                    TaskRunRow(
                        id=task_run_id,
                        test_run_id=state["run_id"],
                        task_id=task.task_id,
                        task_index=index,
                        status=TaskStatus.RUNNING.value,
                        cycle_count=0,
                        started_at=now(),
                    )
                )
            await session.commit()
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=task_run_id,
            event_type="task_started",
            payload={"task_index": index, "task": task.model_dump(mode="json")},
            dedup_key=f"{state['run_id']}:{task_run_id}:task_started",
        )
        return {
            "route": "check",
            "current_task_run_id": task_run_id,
            "cycle_count": 0,
            "latest_artifact_id": None,
        }

    async def _check_cycle(self, state: GraphState) -> dict[str, Any]:
        cycle = state["cycle_count"] + 1
        task = Task.model_validate(state["tasks"][state["task_index"]])
        async with self.sessions() as session:
            row = await session.get(TaskRunRow, state["current_task_run_id"])
            row.cycle_count = min(cycle, task.max_cycles)
            await session.commit()
        if cycle > task.max_cycles:
            await self._finish_task(
                state,
                TaskStatus.FAILED,
                f"Reached max_cycles={task.max_cycles}",
            )
            return {"route": "skip", "cycle_count": cycle}
        return {"route": "observe", "cycle_count": cycle}

    async def _capture_observation(self, state: GraphState) -> dict[str, Any]:
        if self.registry.cancellation(state["run_id"]).is_set():
            return {"route": "finalize"}
        run = await self._run_row(state["run_id"])
        device = self._device_for(run)
        screenshot = None
        last_error: Exception | None = None
        for _ in range(3):
            try:
                screenshot = await device.screenshot()
                break
            except Exception as exc:
                last_error = exc
        if screenshot is None:
            await self._finish_task(
                state, TaskStatus.BLOCKED, f"Screenshot failed: {last_error}"
            )
            await self.events.append(
                run_id=state["run_id"],
                task_run_id=state["current_task_run_id"],
                event_type="error",
                payload={"classification": "CaptureFailed", "message": str(last_error)},
                dedup_key=self._dedup(state, "capture_error"),
            )
            return {"route": "skip"}
        artifact = await self.artifacts.save(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            artifact_type="screenshot",
            content=screenshot.content,
            mime_type=screenshot.mime_type,
            extension="png",
        )
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            event_type="observation_captured",
            payload={
                "artifact_id": artifact.id,
                "activity": screenshot.activity,
                "cycle_count": state["cycle_count"],
            },
            dedup_key=self._dedup(state, "observation_captured"),
        )
        return {"route": "decide", "latest_artifact_id": artifact.id}

    async def _decide(self, state: GraphState) -> dict[str, Any]:
        task = Task.model_validate(state["tasks"][state["task_index"]])
        async with self.sessions() as session:
            artifact = await session.get(ArtifactRow, state["latest_artifact_id"])
        content = self.artifacts.resolve(artifact.relative_path).read_bytes()
        ctx = DecisionContext(
            task=task,
            artifact_id=artifact.id,
            screenshot_bytes=content,
            screenshot_mime_type=artifact.mime_type,
            capabilities=DeviceCapabilities.model_validate(state["capabilities"]),
            tools=self.tools.list_tools(),
            recent_history=state.get("recent_history", [])[-10:],
            cycle_count=state["cycle_count"],
            max_cycles=task.max_cycles,
        )
        decision = None
        last_error: Exception | None = None
        for _ in range(3):
            try:
                candidate = await self.llm.decide(ctx)
                self._validate_decision(task, candidate, artifact.id)
                decision = candidate
                break
            except Exception as exc:
                last_error = exc
        if decision is None:
            await self._finish_task(
                state, TaskStatus.BLOCKED, f"Invalid decision: {last_error}"
            )
            await self.events.append(
                run_id=state["run_id"],
                task_run_id=state["current_task_run_id"],
                event_type="error",
                payload={"classification": "InvalidDecision", "message": str(last_error)},
                dedup_key=self._dedup(state, "decision_error"),
            )
            return {"route": "skip"}
        payload = decision.model_dump(mode="json")
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            event_type="decision_made",
            payload={"decision": payload, "cycle_count": state["cycle_count"]},
            dedup_key=self._dedup(state, "decision_made"),
        )
        history = (state.get("recent_history", []) + [payload])[-10:]
        if isinstance(decision, ActionDecision):
            return {
                "route": "execute",
                "latest_decision_summary": decision.summary,
                "recent_history": history,
            }
        if isinstance(decision, TaskPassDecision):
            await self._finish_task(state, TaskStatus.PASSED, decision.summary)
            return {
                "route": "select",
                "task_index": state["task_index"] + 1,
                "current_task_run_id": None,
                "latest_decision_summary": decision.summary,
                "recent_history": history,
            }
        status = (
            TaskStatus.FAILED
            if isinstance(decision, TaskFailDecision)
            else TaskStatus.BLOCKED
        )
        await self._finish_task(state, status, decision.summary)
        return {
            "route": "skip",
            "latest_decision_summary": decision.summary,
            "recent_history": history,
        }

    def _validate_decision(
        self, task: Task, decision: Any, latest_artifact_id: str
    ) -> None:
        if task.type == TaskType.JUDGE and isinstance(decision, ActionDecision):
            if not isinstance(decision.action, WaitAction):
                raise ValueError("Judge tasks only allow WAIT actions")
        if isinstance(decision, (TaskPassDecision, TaskFailDecision)):
            if latest_artifact_id not in decision.evidence_artifact_ids:
                raise ValueError("Terminal decision must reference the latest screenshot")
        if isinstance(decision, ActionDecision):
            action = decision.action
            if action.type == "PRESS_KEY":
                # Device capability validation is repeated at execution time.
                return
            if isinstance(action, ToolAction):
                allowed = {tool.name for tool in self.tools.list_tools()}
                if action.tool_name not in allowed:
                    raise ValueError(f"Tool is not enabled: {action.tool_name}")

    async def _execute_action(self, state: GraphState) -> dict[str, Any]:
        if self.registry.cancellation(state["run_id"]).is_set():
            return {"route": "finalize"}
        # The action is read from the persisted decision event/recent history,
        # which keeps large model responses out of graph state.
        decision = state["recent_history"][-1]
        action = decision["action"]
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            event_type="action_started",
            payload={"action": action, "cycle_count": state["cycle_count"]},
            dedup_key=self._dedup(state, "action_started"),
        )
        run = await self._run_row(state["run_id"])
        device = self._device_for(run)
        try:
            result = await asyncio.wait_for(
                self._dispatch_action(device, run, state, action),
                timeout=self.action_timeout_seconds,
            )
        except (TimeoutError, ActionTimeout) as exc:
            await self.events.append(
                run_id=state["run_id"],
                task_run_id=state["current_task_run_id"],
                event_type="error",
                payload={"classification": "ActionTimeout", "message": str(exc)},
                dedup_key=self._dedup(state, "action_timeout"),
            )
            return {"route": "check"}
        except Exception as exc:
            await self._finish_task(state, TaskStatus.BLOCKED, str(exc))
            await self.events.append(
                run_id=state["run_id"],
                task_run_id=state["current_task_run_id"],
                event_type="error",
                payload={"classification": type(exc).__name__, "message": str(exc)},
                dedup_key=self._dedup(state, "action_error"),
            )
            return {"route": "skip"}
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            event_type="action_finished",
            payload={
                "action": action,
                "result": result.model_dump(mode="json"),
                "cycle_count": state["cycle_count"],
            },
            dedup_key=self._dedup(state, "action_finished"),
        )
        history = (
            state.get("recent_history", [])
            + [{"action": action, "result": result.model_dump(mode="json")}]
        )[-10:]
        return {"route": "check", "recent_history": history}

    async def _dispatch_action(
        self,
        device: DeviceController,
        run: TestRunRow,
        state: GraphState,
        action: dict[str, Any],
    ) -> Any:
        action_type = action["type"]
        if action_type == "PRESS_KEY":
            capabilities = DeviceCapabilities.model_validate(state["capabilities"])
            from app.domain.models import RemoteKey

            key = RemoteKey(action["key"])
            if key not in capabilities.supported_keys:
                raise ValueError(f"Device does not support {key.value}")
            return await device.press(key)
        if action_type == "INPUT_TEXT":
            return await device.input_text(action["text"])
        if action_type == "WAIT":
            return await device.wait(action["duration_ms"])
        if action_type == "TOOL":
            last_error: Exception | None = None
            for _ in range(3):
                try:
                    return await self.tools.execute(
                        action["tool_name"],
                        action.get("arguments", {}),
                        ToolContext(
                            run_id=run.id,
                            task_run_id=state["current_task_run_id"],
                            device_id=run.device_id,
                        ),
                    )
                except TransientToolError as exc:
                    last_error = exc
            raise last_error or RuntimeError("Tool execution failed")
        raise ValueError(f"Unknown action type: {action_type}")

    async def _finish_task(
        self, state: GraphState, status: TaskStatus, summary: str
    ) -> None:
        async with self.sessions() as session:
            row = await session.get(TaskRunRow, state["current_task_run_id"])
            row.status = status.value
            row.summary = summary
            row.cycle_count = min(
                state["cycle_count"],
                Task.model_validate(state["tasks"][state["task_index"]]).max_cycles,
            )
            row.finished_at = now()
            await session.commit()
        await self.events.append(
            run_id=state["run_id"],
            task_run_id=state["current_task_run_id"],
            event_type="task_finished",
            payload={
                "status": status.value,
                "summary": summary,
                "cycle_count": state["cycle_count"],
            },
            dedup_key=self._dedup(state, f"task_finished:{status.value}"),
        )

    async def _skip_remaining(self, state: GraphState) -> dict[str, Any]:
        start = state["task_index"] + 1
        skipped: list[str] = []
        async with self.sessions() as session:
            for index in range(start, len(state["tasks"])):
                task = Task.model_validate(state["tasks"][index])
                existing = await session.scalar(
                    select(TaskRunRow).where(
                        TaskRunRow.test_run_id == state["run_id"],
                        TaskRunRow.task_index == index,
                    )
                )
                if not existing:
                    session.add(
                        TaskRunRow(
                            id=str(uuid4()),
                            test_run_id=state["run_id"],
                            task_id=task.task_id,
                            task_index=index,
                            status=TaskStatus.SKIPPED.value,
                            cycle_count=0,
                            summary="Skipped by global fail-fast",
                            finished_at=now(),
                        )
                    )
                    skipped.append(task.task_id)
            await session.commit()
        if skipped:
            await self.events.append(
                run_id=state["run_id"],
                event_type="tasks_skipped",
                payload={"task_ids": skipped, "reason": "global_fail_fast"},
                dedup_key=f"{state['run_id']}:tasks_skipped",
            )
        return {"route": "finalize"}

    async def _finalize_run(self, state: GraphState) -> dict[str, Any]:
        cancelled = self.registry.cancellation(state["run_id"]).is_set()
        async with self.sessions() as session:
            run = await session.get(TestRunRow, state["run_id"])
            task_runs = list(
                (
                    await session.scalars(
                        select(TaskRunRow)
                        .where(TaskRunRow.test_run_id == state["run_id"])
                        .order_by(TaskRunRow.task_index)
                    )
                ).all()
            )
            if cancelled:
                result = OverallResult.CANCELLED
                run.status = RunStatus.CANCELLED.value
                for task_run in task_runs:
                    if task_run.status == TaskStatus.RUNNING.value:
                        task_run.status = TaskStatus.SKIPPED.value
                        task_run.summary = "Cancelled by user"
                        task_run.finished_at = now()
            elif any(item.status == TaskStatus.FAILED.value for item in task_runs):
                result = OverallResult.FAIL
                run.status = RunStatus.FINISHED.value
            elif state.get("run_blocked") or any(
                item.status == TaskStatus.BLOCKED.value for item in task_runs
            ):
                result = OverallResult.BLOCKED
                run.status = RunStatus.FINISHED.value
            elif task_runs and all(
                item.status == TaskStatus.PASSED.value for item in task_runs
            ):
                result = OverallResult.PASS
                run.status = RunStatus.FINISHED.value
            else:
                result = OverallResult.BLOCKED
                run.status = RunStatus.FINISHED.value
            run.overall_result = result.value
            run.finished_at = now()
            await session.commit()
        event_type = "run_cancelled" if cancelled else "run_finished"
        await self.events.append(
            run_id=state["run_id"],
            event_type=event_type,
            payload={"status": run.status, "overall_result": result.value},
            dedup_key=f"{state['run_id']}:{event_type}",
        )
        return {"route": "end"}

    async def _unexpected_failure(self, run_id: str, exc: Exception) -> None:
        async with self.sessions() as session:
            run = await session.get(TestRunRow, run_id)
            if not run or run.status in {
                RunStatus.FINISHED.value,
                RunStatus.CANCELLED.value,
            }:
                return
            active = await session.scalar(
                select(TaskRunRow).where(
                    TaskRunRow.test_run_id == run_id,
                    TaskRunRow.status == TaskStatus.RUNNING.value,
                )
            )
            if active:
                active.status = TaskStatus.BLOCKED.value
                active.summary = str(exc)
                active.finished_at = now()
            run.status = RunStatus.FINISHED.value
            run.overall_result = OverallResult.BLOCKED.value
            run.finished_at = now()
            await session.commit()
        await self.events.append(
            run_id=run_id,
            event_type="error",
            payload={"classification": type(exc).__name__, "message": str(exc)},
            dedup_key=f"{run_id}:unexpected_failure",
        )
        await self.events.append(
            run_id=run_id,
            event_type="run_finished",
            payload={
                "status": RunStatus.FINISHED.value,
                "overall_result": OverallResult.BLOCKED.value,
            },
            dedup_key=f"{run_id}:run_finished",
        )

    @staticmethod
    def _dedup(state: GraphState, event_type: str) -> str:
        return (
            f"{state['run_id']}:{state.get('current_task_run_id')}:"
            f"{state.get('cycle_count', 0)}:{event_type}"
        )
