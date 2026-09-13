from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import add_messages
from langsmith import tracing_context

from app.domain.errors import (
    describe_exception,
    TaskAgentCancelled,
    ToolCallError,
    ModelContextBudgetExceeded,
    ModelProfileMismatch,
    ModelCallTimeout,
    PromptVersionMismatch,
)
from app.domain.execution import (
    ExecutionErrorEvent,
    MessageAppendedEvent,
    MessageValidationFailedEvent,
    ReasonCode,
    RunEvent,
    ToolStartedEvent,
    TaskAgentCompletion,
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
    TestRunVerdict,
    aggregate_test_run_verdict,
)
from app.domain.planning import TestTask
from app.domain.resources.llm import LLMProfileSnapshot
from app.execution.signals import (
    GRAPH_SIGNAL_ADAPTER,
    MessageValidationSignal,
    ToolStartedSignal,
)
from app.execution.task_agent import TaskAgentFactory
from app.persistence.test_repository import SqlAlchemyTestRepository
from app.event_stream.projector import project_run_message


class RunExecutor:
    def __init__(
        self,
        *,
        repository: SqlAlchemyTestRepository,
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
        on_cleanup_failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        active_task: TaskRun | None = None
        remaining_after_active_task: list[TestTask] = []
        cancellation_completed = False
        try:
            detail = await self.repository.get_test_run_detail(test_run_id)
            await self.repository.start_run(test_run_id)
            completed: list[TaskRun] = []
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as checkpointer:
                with tracing_context(enabled=False):
                    for index, test_task in enumerate(detail.test_plan.content.tasks):
                        if run_cancellation_event.is_set():
                            cancellation_completed = True
                            break
                        active_task = await self.repository.start_task(
                            test_run_id, test_task
                        )
                        remaining_after_active_task = list(
                            detail.test_plan.content.tasks[index + 1 :]
                        )
                        completion = await self._execute_task(
                            test_run_id=test_run_id,
                            task_run=active_task,
                            test_task=test_task,
                            tools=tools,
                            screenshot_history_rounds=detail.snapshot.screenshot_history_rounds,
                            previous_task_runs=completed,
                            expected_model_profile=(
                                detail.snapshot.execution_model
                            ),
                            expected_prompt_version=(
                                detail.snapshot.act_prompt_version
                                if test_task.definition.type.value == "act"
                                else detail.snapshot.judge_prompt_version
                            ),
                            run_cancellation_event=run_cancellation_event,
                            checkpointer=checkpointer,
                            on_cleanup_failure=on_cleanup_failure,
                        )
                        await self.repository.finish_task(
                            active_task.id,
                            status=completion.status,
                            result=completion.result,
                            cycle_count=completion.cycle_count,
                        )
                        active_task = (
                            await self.repository.list_task_runs(test_run_id)
                        )[-1]
                        completed.append(active_task)
                        if completion.result.reason_code == ReasonCode.USER_CANCELLED:
                            cancellation_completed = True
                            break
                        if completion.status in {
                            TaskRunStatus.FAILED,
                            TaskRunStatus.BLOCKED,
                        }:
                            if remaining_after_active_task:
                                await self.repository.skip_remaining(
                                    test_run_id,
                                    remaining_after_active_task,
                                )
                            break
                        active_task = None
            task_runs = await self.repository.list_task_runs(test_run_id)
            verdict = (TestRunVerdict.CANCELLED if cancellation_completed
                       else aggregate_test_run_verdict(task_runs))
        except Exception as exc:
            cancelled = isinstance(exc, TaskAgentCancelled)
            reason = ReasonCode.USER_CANCELLED if cancelled else self._reason_for_exception(exc)
            summary = str(exc) if cancelled else describe_exception(exc, phase="Task agent failed")
            if active_task is not None:
                latest = await self.repository.list_task_runs(test_run_id)
                current = next(
                    (task for task in latest if task.id == active_task.id), active_task
                )
                if current.status == TaskRunStatus.RUNNING:
                    await self.repository.finish_task(
                        current.id,
                        status=TaskRunStatus.CANCELLED if cancelled else TaskRunStatus.BLOCKED,
                        result=TaskRunResult(
                            reason_code=reason,
                            summary=summary,
                            evidence_artifact_ids=[],
                        ),
                        cycle_count=current.cycle_count,
                    )
                    if remaining_after_active_task and not cancelled:
                        await self.repository.skip_remaining(
                            test_run_id,
                            remaining_after_active_task,
                        )
            if not cancelled:
                await self._report_execution_error(test_run_id, reason, summary)
            verdict = TestRunVerdict.CANCELLED if cancelled else TestRunVerdict.BLOCKED

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
        task_run: TaskRun,
        test_task: TestTask,
        tools: tuple[BaseTool, ...],
        screenshot_history_rounds: int,
        previous_task_runs: list[TaskRun],
        expected_model_profile: LLMProfileSnapshot,
        expected_prompt_version: str,
        run_cancellation_event: asyncio.Event,
        checkpointer: BaseCheckpointSaver[Any],
        on_cleanup_failure: Callable[[BaseException], None] | None = None,
    ) -> TaskAgentCompletion:
        graph, initial = await self.agent_factory.build(
            task_run_id=task_run.id,
            tools=tools,
            screenshot_history_rounds=screenshot_history_rounds,
            task=test_task,
            previous_task_runs=previous_task_runs,
            expected_model_profile=expected_model_profile,
            expected_prompt_version=expected_prompt_version,
            run_cancellation_event=run_cancellation_event,
            checkpointer=checkpointer,
            on_cleanup_failure=on_cleanup_failure,
        )
        completion: TaskAgentCompletion | None = None
        async for part in graph.astream(
            initial,
            config={"configurable": {"thread_id": f"task-run:{task_run.id}"},
                    "recursion_limit": test_task.definition.max_cycles * 8 + 20},
            stream_mode=["updates", "custom"],
            version="v2",
            durability="exit",
        ):
            mode = part["type"]
            data = part["data"]
            if mode == "updates":
                completion = await self._consume_updates(
                    test_run_id, task_run.id, data, completion
                )
            elif mode == "custom":
                await self._consume_signal(test_run_id, task_run.id, data)
        if completion is None:
            raise RuntimeError("TaskAgent ended without TaskAgentCompletion")
        return completion

    async def _consume_updates(
        self,
        test_run_id: str,
        task_run_id: str,
        data: object,
        completion: TaskAgentCompletion | None,
    ) -> TaskAgentCompletion | None:
        if not isinstance(data, dict):
            raise TypeError("LangGraph updates stream must contain a node mapping")
        for node_name, update in data.items():
            # Only application-owned publication boundaries expose messages.
            # ToolNode first writes raw results to State;
            # after_tools saves evidence and returns the same-ID public version.
            if not isinstance(update, dict) or node_name not in {
                "initialize", "guard", "model", "validate", "announce_tool", "after_tools",
            }:
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
            cycle_count = update.get("cycle_count")
            if isinstance(cycle_count, int) and cycle_count > 0:
                await self.repository.update_cycle(task_run_id, cycle_count)
            value = update.get("completion")
            if value is not None:
                completion = (
                    value
                    if isinstance(value, TaskAgentCompletion)
                    else TaskAgentCompletion.model_validate(value)
                )
        return completion

    async def _consume_signal(
        self, test_run_id: str, task_run_id: str, data: object
    ) -> None:
        signal = GRAPH_SIGNAL_ADAPTER.validate_python(data)
        if isinstance(signal, MessageValidationSignal):
            event = MessageValidationFailedEvent(
                test_run_id=test_run_id,
                task_run_id=task_run_id,
                message_id=signal.message_id,
                attempt=signal.attempt,
                reason=signal.reason,
            )
            suffix = f"validation:{signal.message_id}:{signal.attempt}"
        elif isinstance(signal, ToolStartedSignal):
            event = ToolStartedEvent(
                test_run_id=test_run_id,
                task_run_id=task_run_id,
                call_id=signal.call_id,
            )
            suffix = f"tool.started:{signal.call_id}"
        else:
            raise TypeError(f"unsupported graph signal: {type(signal).__name__}")
        await self.repository.append_event(
            event, dedup_key=f"{test_run_id}:{task_run_id}:{suffix}"
        )

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
        if isinstance(exc, PromptVersionMismatch):
            return ReasonCode.PROMPT_VERSION_MISMATCH
        if isinstance(exc, ModelProfileMismatch):
            return ReasonCode.MODEL_PROFILE_MISMATCH
        if isinstance(exc, ModelContextBudgetExceeded):
            return ReasonCode.MODEL_CONTEXT_EXCEEDED
        if isinstance(exc, (ToolCallError, TimeoutError)):
            return ReasonCode.TOOL_FAILED
        return ReasonCode.UNEXPECTED_ERROR
