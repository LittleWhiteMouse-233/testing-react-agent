from __future__ import annotations

from collections import Counter
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langsmith import tracing_context

from app.domain.errors import ReasonCode
from app.domain.events import (
    ExecutionErrorEvent,
    RunCancelledEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TaskFinishedEvent,
    TaskStartedEvent,
    TasksSkippedEvent,
)
from app.domain.execution import (
    OverallResult,
    RunOutcome,
    TaskExecution,
    TaskOutcome,
    TaskStatus,
)
from app.execution.ports import ExecutionJournal, ExecutionRepository, TaskAgentFactory
from app.services.registry import RunRegistry


class RunExecutor:
    def __init__(
        self,
        *,
        repository: ExecutionRepository,
        journal: ExecutionJournal,
        agent_factory: TaskAgentFactory,
        registry: RunRegistry,
        checkpoint_path: str,
    ) -> None:
        self.repository = repository
        self.journal = journal
        self.agent_factory = agent_factory
        self.registry = registry
        self.checkpoint_path = checkpoint_path

    async def run(self, run_id: str) -> None:
        active_task_run_id: str | None = None
        try:
            snapshot = await self.repository.load_snapshot(run_id)
            await self.repository.start_run(
                run_id,
                RunStartedEvent(
                    run_id=run_id,
                    device_id=snapshot.device.id,
                ),
            )
            if snapshot.device.capabilities is None:
                await self._finish_blocked_run(
                    run_id,
                    ReasonCode.DEVICE_UNAVAILABLE,
                    snapshot.device.health_message or "Device is unavailable",
                )
                return
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as checkpointer:
                with tracing_context(enabled=False):
                    for index, task in enumerate(snapshot.plan.tasks):
                        if self.registry.cancellation(run_id).is_set():
                            await self._finish_cancelled(run_id)
                            return
                        pending_execution = TaskExecution(
                            id=str(uuid4()),
                            task=task,
                            task_index=index,
                            status=TaskStatus.RUNNING,
                        )
                        task_execution = await self.repository.start_task(
                            run_id,
                            pending_execution,
                            TaskStartedEvent(
                                run_id=run_id,
                                task_run_id=pending_execution.id,
                                task_index=index,
                                task=task,
                            ),
                        )
                        active_task_run_id = task_execution.id
                        context = await self.repository.recent_context(run_id, 10)
                        agent = self.agent_factory.build(
                            run_id=run_id,
                            task_run_id=task_execution.id,
                            device_id=snapshot.device.id,
                            task=task,
                            capabilities=snapshot.device.capabilities,
                            enabled_tool_names={
                                item.name for item in snapshot.enabled_tools
                            },
                            cross_task_context=context,
                            checkpointer=checkpointer,
                        )
                        try:
                            state = await agent.ainvoke(
                                {
                                    "messages": [
                                        HumanMessage(
                                            content=(
                                                f"Begin {task.type.value} task: "
                                                f"{task.title}"
                                            )
                                        )
                                    ],
                                    "task": task.model_dump(mode="json"),
                                    "policy_id": task.type.value,
                                    "cycle_count": 0,
                                    "model_attempt": 0,
                                    "latest_observation": None,
                                    "latest_tool_result": None,
                                    "terminal_outcome": None,
                                    "route": "observe",
                                },
                                config={
                                    "configurable": {
                                        "thread_id": f"execution-v2:{task_execution.id}"
                                    }
                                },
                            )
                            outcome = TaskOutcome.model_validate(
                                state.get("terminal_outcome")
                            )
                        except Exception as exc:
                            cycle_count = await self._current_cycle(
                                run_id, task_execution.id
                            )
                            outcome = TaskOutcome(
                                status=TaskStatus.BLOCKED,
                                reason_code=ReasonCode.UNEXPECTED_ERROR,
                                summary=f"Task agent failed unexpectedly: {exc}",
                                cycle_count=cycle_count,
                            )
                            await self.journal.append(
                                ExecutionErrorEvent(
                                    run_id=run_id,
                                    task_run_id=task_execution.id,
                                    reason_code=ReasonCode.UNEXPECTED_ERROR,
                                    message=str(exc),
                                ),
                                dedup_key=(f"{run_id}:{task_execution.id}:unexpected"),
                            )
                        await self.repository.finish_task(
                            task_execution.id,
                            outcome,
                            TaskFinishedEvent(
                                run_id=run_id,
                                task_run_id=task_execution.id,
                                outcome=outcome,
                            ),
                        )
                        active_task_run_id = None
                        if outcome.reason_code == ReasonCode.USER_CANCELLED:
                            await self._finish_cancelled(run_id)
                            return
                        if outcome.status in {
                            TaskStatus.FAILED,
                            TaskStatus.BLOCKED,
                        }:
                            skipped_executions: list[TaskExecution] = []
                            for skipped_index in range(
                                index + 1, len(snapshot.plan.tasks)
                            ):
                                skipped_task = snapshot.plan.tasks[skipped_index]
                                skipped_outcome = TaskOutcome(
                                    status=TaskStatus.SKIPPED,
                                    reason_code=ReasonCode.GLOBAL_FAIL_FAST,
                                    summary="Skipped by global fail-fast",
                                    cycle_count=0,
                                )
                                skipped_executions.append(
                                    TaskExecution(
                                        id=str(uuid4()),
                                        task=skipped_task,
                                        task_index=skipped_index,
                                        status=TaskStatus.SKIPPED,
                                        outcome=skipped_outcome,
                                    )
                                )
                            if skipped_executions:
                                await self.repository.skip_remaining(
                                    run_id,
                                    skipped_executions,
                                    TasksSkippedEvent(
                                        run_id=run_id,
                                        task_ids=[
                                            item.task.task_id
                                            for item in skipped_executions
                                        ],
                                    ),
                                )
                            break
            outcome = await self._aggregate(run_id)
            await self.repository.finish_run(
                run_id,
                outcome.result,
                RunFinishedEvent(run_id=run_id, result=outcome.result),
            )
        except Exception as exc:
            if active_task_run_id is not None:
                cycle_count = await self._current_cycle(run_id, active_task_run_id)
                outcome = TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.UNEXPECTED_ERROR,
                    summary=str(exc),
                    cycle_count=cycle_count,
                )
                await self.repository.finish_task(
                    active_task_run_id,
                    outcome,
                    TaskFinishedEvent(
                        run_id=run_id,
                        task_run_id=active_task_run_id,
                        outcome=outcome,
                    ),
                )
            await self._finish_blocked_run(
                run_id, ReasonCode.UNEXPECTED_ERROR, str(exc)
            )
        finally:
            self.registry.unregister(run_id)

    async def _aggregate(self, run_id: str) -> RunOutcome:
        tasks = await self.repository.list_task_executions(run_id)
        statuses = [item.status for item in tasks]
        if any(status == TaskStatus.FAILED for status in statuses):
            result = OverallResult.FAIL
        elif any(
            status in {TaskStatus.BLOCKED, TaskStatus.SKIPPED} for status in statuses
        ):
            result = OverallResult.BLOCKED
        elif statuses and all(status == TaskStatus.PASSED for status in statuses):
            result = OverallResult.PASS
        else:
            result = OverallResult.BLOCKED
        counts = Counter(status.value for status in statuses)
        return RunOutcome(result=result, task_counts=dict(counts))

    async def _current_cycle(self, run_id: str, task_run_id: str) -> int:
        executions = await self.repository.list_task_executions(run_id)
        return next(
            (
                execution.cycle_count
                for execution in executions
                if execution.id == task_run_id
            ),
            0,
        )

    async def _finish_cancelled(self, run_id: str) -> None:
        await self.repository.finish_run(
            run_id,
            OverallResult.CANCELLED,
            RunCancelledEvent(run_id=run_id),
            cancelled=True,
        )

    async def _finish_blocked_run(
        self, run_id: str, reason: ReasonCode, message: str
    ) -> None:
        await self.journal.append(
            ExecutionErrorEvent(
                run_id=run_id,
                reason_code=reason,
                message=message,
            ),
            dedup_key=f"{run_id}:run.error:{reason.value}",
        )
        await self.repository.finish_run(
            run_id,
            OverallResult.BLOCKED,
            RunFinishedEvent(run_id=run_id, result=OverallResult.BLOCKED),
        )
