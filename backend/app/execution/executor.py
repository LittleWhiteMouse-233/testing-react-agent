from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, cast

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import add_messages
from langsmith import tracing_context
from mcp import MCPError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.errors import (
    describe_exception,
    ToolCallError,
    ModelContextBudgetExceeded,
    ModelCallTimeout,
)
from app.domain.execution import (
    ExecutionErrorEvent,
    MessageAppendedEvent,
    ReasonCode,
    RunEvent,
    TaskAgentCompletion,
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
    TestRunVerdict,
    aggregate_test_run_verdict,
)
from app.domain.planning import TestTask, TestTaskType
from app.execution.task_agent import MESSAGE_PUBLISHING_NODES, TaskAgentContext, TaskAgentFactory, TaskAgentInput
from app.execution.task_agent_events import TaskAgentEventRecorder
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.event_stream.projector import project_run_message


class TaskRunMemory(BaseModel):
    """Model-visible projection of a persisted result and its actual execution origin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_no: int = Field(ge=1)
    execution_no: int = Field(ge=1)
    memory: str


_TASK_MEMORIES = TypeAdapter(list[TaskRunMemory])


def serialize_task_memories(memories: Sequence[TaskRunMemory]) -> str:
    """Serialize chronological memory projections without exposing database identities."""
    return _TASK_MEMORIES.dump_json(list(memories)).decode("utf-8")


class RunExecutor:
    def __init__(
        self,
        *,
        repository: SqlAlchemyRunRepository,
        agent_factory: TaskAgentFactory,
        checkpoint_path: str,
    ) -> None:
        self.repository = repository
        self.agent_factory = agent_factory
        self.checkpoint_path = checkpoint_path

    async def run(
        self,
        test_run_id: str,
        run_cancellation_event: asyncio.Event,
        tools: tuple[BaseTool, ...],
    ) -> None:
        active_task: TaskRun | None = None
        remaining_after_active_task: list[TestTask] = []
        cancellation_completed = False
        try:
            detail = await self.repository.get_test_run_detail(test_run_id)
            await self.repository.start_run(test_run_id)
            task_memories: list[TaskRunMemory] = []
            execution_counts: Counter[str] = Counter()
            goal_failure_counts: Counter[str] = Counter()
            test_tasks = detail.test_plan.content.tasks
            task_index = 0
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as checkpointer:
                with tracing_context(enabled=False):
                    while task_index < len(test_tasks):
                        if run_cancellation_event.is_set():
                            cancellation_completed = True
                            break
                        test_task = test_tasks[task_index]
                        active_task = await self.repository.start_task(
                            test_run_id, test_task
                        )
                        execution_counts[test_task.test_task_id] += 1
                        remaining_after_active_task = test_tasks[task_index + 1 :]
                        task_input = TaskAgentInput(
                            task_context=TaskAgentContext(
                                task_run_id=active_task.id,
                                task=test_task,
                                memory_text=serialize_task_memories(task_memories),
                                max_cycles=detail.environment.max_cycles_by_task_type[test_task.definition.type],
                            ),
                            tools=tools,
                            resource_catalog=detail.environment.resource_catalog,
                            model_profile_id=detail.environment.execution_model.profile_id,
                        )
                        completion = await self._execute_task(
                            test_run_id=test_run_id,
                            task_input=task_input,
                            run_cancellation_event=run_cancellation_event,
                            checkpointer=checkpointer,
                        )
                        is_goal_failure = (test_task.definition.type == TestTaskType.GOAL
                                       and completion.status == TaskRunStatus.FAILED)
                        if is_goal_failure:
                            goal_failure_counts[test_task.test_task_id] += 1
                        if is_goal_failure and goal_failure_counts[test_task.test_task_id] >= detail.environment.goal_max_failures:
                            completion = completion.model_copy(update={"result": completion.result.model_copy(
                                update={"reason_code": ReasonCode.GOAL_FAILURE_LIMIT},
                            )})
                        active_task = await self.repository.finish_task(
                            active_task.id,
                            status=completion.status,
                            result=completion.result,
                            cycle_count=completion.cycle_count,
                        )
                        if active_task.result is not None and active_task.result.memory is not None:
                            task_memories.append(TaskRunMemory(
                                task_no=test_task.definition.task_no,
                                execution_no=execution_counts[test_task.test_task_id],
                                memory=active_task.result.memory,
                            ))
                        if completion.result.reason_code == ReasonCode.USER_CANCELLED or run_cancellation_event.is_set():
                            cancellation_completed = True
                            break
                        if (completion.status == TaskRunStatus.BLOCKED
                            or completion.result.reason_code == ReasonCode.GOAL_FAILURE_LIMIT
                            or (completion.status == TaskRunStatus.FAILED and test_task.definition.type == TestTaskType.ACT)):
                            if remaining_after_active_task:
                                await self.repository.skip_remaining(
                                    test_run_id,
                                    remaining_after_active_task,
                                )
                            break
                        if is_goal_failure:
                            jump_target = test_task.definition.jump_back_to_task_no
                            assert jump_target is not None
                            task_index = jump_target - 1
                        else:
                            task_index += 1
                        active_task = None
            task_runs = await self.repository.list_task_runs(test_run_id)
            verdict = (TestRunVerdict.CANCELLED if cancellation_completed
                       else aggregate_test_run_verdict(task_runs, test_tasks))
        except Exception as exc:
            reason = self._reason_for_exception(exc)
            summary = describe_exception(exc, phase="Task agent failed")
            if active_task is not None:
                latest = await self.repository.list_task_runs(test_run_id)
                current = next(
                    (task for task in latest if task.id == active_task.id), active_task
                )
                if current.status == TaskRunStatus.RUNNING:
                    await self.repository.finish_task(
                        current.id,
                        status=TaskRunStatus.BLOCKED,
                        result=TaskRunResult(
                            reason_code=reason,
                            summary=summary,
                        ),
                        cycle_count=current.cycle_count,
                    )
                    if remaining_after_active_task:
                        await self.repository.skip_remaining(
                            test_run_id,
                            remaining_after_active_task,
                        )
            await self._report_execution_error(test_run_id, reason, summary)
            verdict = TestRunVerdict.BLOCKED

        # Commit once outside execution error handling. A persistence failure
        # must propagate, not trigger another report or terminal write.
        await self.repository.finish_run(
            test_run_id,
            verdict,
        )

    async def _execute_task(
        self,
        *,
        test_run_id: str,
        task_input: TaskAgentInput,
        run_cancellation_event: asyncio.Event,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> TaskAgentCompletion:
        task_context = task_input.task_context
        graph, initial, runtime_context = await self.agent_factory.build(
            task_input=task_input,
            run_cancellation_event=run_cancellation_event,
            checkpointer=checkpointer,
            event_recorder=TaskAgentEventRecorder(
                repository=self.repository, test_run_id=test_run_id, task_run_id=task_context.task_run_id,
            ),
        )
        completion: TaskAgentCompletion | None = None
        async for part in graph.astream(
            initial,
            context=runtime_context,
            config={"configurable": {"thread_id": f"task-run:{task_context.task_run_id}"},
                    "recursion_limit": task_context.max_cycles * 8 + 20},
            # LangGraph 1.2.9 re-raises handled errors with custom streams (#8608).
            # Product events are awaited directly; framework State uses updates only.
            stream_mode="updates",
            version="v2",
            durability="exit",
        ):
            node_updates = part["data"]
            if not isinstance(node_updates, dict):
                raise TypeError("LangGraph updates stream must contain a node mapping")
            await self._publish_message_updates(test_run_id, task_context.task_run_id, node_updates)
            for update in node_updates.values():
                if not isinstance(update, dict):
                    continue
                value = update.get("completion")
                if value is not None:
                    completion = TaskAgentCompletion.model_validate(value)
        if completion is None:
            raise RuntimeError("TaskAgent ended without TaskAgentCompletion")
        return completion

    async def _publish_message_updates(
        self,
        test_run_id: str,
        task_run_id: str,
        node_updates: Mapping[str, object],
    ) -> None:
        for node_name, update in node_updates.items():
            # Only application-owned publication boundaries expose messages.
            # ToolNode first writes raw results to State;
            # after_tools saves images and returns the same-ID public version.
            if not isinstance(update, dict) or node_name not in MESSAGE_PUBLISHING_NODES:
                continue
            messages = update.get("messages", [])
            if isinstance(messages, BaseMessage):
                messages = [messages]
            normalized_messages = add_messages([], cast(Any, messages))
            message_events: list[tuple[RunEvent, str | None]] = []
            for message in normalized_messages:
                if not isinstance(message, BaseMessage):
                    raise TypeError("updates.messages must contain BaseMessage")
                projected = project_run_message(message)
                message_events.append(
                    (
                        MessageAppendedEvent(
                            test_run_id=test_run_id,
                            task_run_id=task_run_id,
                            message=projected,
                        ),
                        f"{test_run_id}:{task_run_id}:message:{projected.message_id}",
                    )
                )
            if message_events:
                await self.repository.append_events(message_events)

    async def _report_execution_error(
        self, test_run_id: str, reason: ReasonCode, message: str
    ) -> None:
        await self.repository.append_event(
            ExecutionErrorEvent(
                test_run_id=test_run_id,
                task_run_id=None,
                reason_code=reason,
                message=message or reason.value,
            ),
            dedup_key=f"{test_run_id}:run.error:{reason.value}",
        )

    @staticmethod
    def _reason_for_exception(exc: Exception) -> ReasonCode:
        if isinstance(exc, ModelCallTimeout):
            return ReasonCode.MODEL_UNAVAILABLE
        if isinstance(exc, ModelContextBudgetExceeded):
            return ReasonCode.MODEL_CONTEXT_EXCEEDED
        if isinstance(exc, (ToolCallError, MCPError, TimeoutError)):
            return ReasonCode.TOOL_FAILED
        return ReasonCode.UNEXPECTED_ERROR
