from __future__ import annotations

import asyncio
from typing import Any, cast

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeTimeoutError
from langgraph.graph import add_messages
from langsmith import tracing_context

from app.domain.errors import (
    ActionTimeout,
    CaptureFailed,
    DeviceUnavailable,
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
)
from app.domain.execution import (
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
    ) -> None:
        active_task: TaskRun | None = None
        remaining_after_active_task: list[TestTask] = []
        try:
            detail = await self.repository.get_test_run_detail(test_run_id)
            await self.repository.start_run(test_run_id)
            if not detail.snapshot.device_environment.health.available:
                await self._block_run(
                    test_run_id,
                    ReasonCode.DEVICE_UNAVAILABLE,
                    detail.snapshot.device_environment.health.message
                    or "Device is unavailable",
                )
                return
            completed: list[TaskRun] = []
            async with AsyncSqliteSaver.from_conn_string(
                self.checkpoint_path
            ) as checkpointer:
                with tracing_context(enabled=False):
                    for index, test_task in enumerate(detail.test_plan.content.tasks):
                        if run_cancellation_event.is_set():
                            await self.repository.finish_run(
                                test_run_id, TestRunVerdict.CANCELLED
                            )
                            return
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
                            device_id=detail.run.device_id,
                            previous_task_runs=completed,
                            expected_model_profile=(
                                detail.snapshot.act_model
                                if test_task.definition.type.value == "act"
                                else detail.snapshot.judge_model
                            ),
                            expected_prompt_version=(
                                detail.snapshot.act_prompt_version
                                if test_task.definition.type.value == "act"
                                else detail.snapshot.judge_prompt_version
                            ),
                            run_cancellation_event=run_cancellation_event,
                            checkpointer=checkpointer,
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
                            await self.repository.finish_run(
                                test_run_id, TestRunVerdict.CANCELLED
                            )
                            return
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
            await self.repository.finish_run(
                test_run_id, aggregate_test_run_verdict(task_runs)
            )
        except Exception as exc:
            reason = self._reason_for_exception(exc)
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
                            summary=f"Task agent failed: {exc}",
                            evidence_artifact_ids=[],
                        ),
                        cycle_count=current.cycle_count,
                    )
                    if remaining_after_active_task:
                        await self.repository.skip_remaining(
                            test_run_id,
                            remaining_after_active_task,
                        )
            await self._block_run(test_run_id, reason, str(exc))

    async def _execute_task(
        self,
        *,
        test_run_id: str,
        task_run: TaskRun,
        test_task: TestTask,
        device_id: str,
        previous_task_runs: list[TaskRun],
        expected_model_profile: LLMProfileSnapshot,
        expected_prompt_version: str,
        run_cancellation_event: asyncio.Event,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> TaskAgentCompletion:
        graph, initial = await self.agent_factory.build(
            task_run_id=task_run.id,
            device_id=device_id,
            task=test_task,
            previous_task_runs=previous_task_runs,
            expected_model_profile=expected_model_profile,
            expected_prompt_version=expected_prompt_version,
            run_cancellation_event=run_cancellation_event,
            checkpointer=checkpointer,
        )
        completion: TaskAgentCompletion | None = None
        async for part in graph.astream(
            initial,
            config={"configurable": {"thread_id": f"task-run:{task_run.id}"}},
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
        for update in data.values():
            if not isinstance(update, dict):
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

    async def _block_run(
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
        run = await self.repository.get_test_run(test_run_id)
        if run.status.value != "finished":
            await self.repository.finish_run(test_run_id, TestRunVerdict.BLOCKED)

    @staticmethod
    def _reason_for_exception(exc: Exception) -> ReasonCode:
        if isinstance(exc, DeviceUnavailable):
            return ReasonCode.DEVICE_UNAVAILABLE
        if isinstance(exc, (CaptureFailed, ActionTimeout)):
            return ReasonCode.CAPTURE_FAILED
        if isinstance(exc, ModelCallTimeout):
            return ReasonCode.MODEL_UNAVAILABLE
        if isinstance(exc, PromptVersionMismatch):
            return ReasonCode.PROMPT_VERSION_MISMATCH
        if isinstance(exc, ModelProfileMismatch):
            return ReasonCode.MODEL_PROFILE_MISMATCH
        if isinstance(exc, ModelContextBudgetExceeded):
            return ReasonCode.MODEL_CONTEXT_EXCEEDED
        if isinstance(exc, NodeTimeoutError) and exc.node == "tools":
            return ReasonCode.TOOL_FAILED
        if isinstance(exc, TimeoutError):
            return ReasonCode.TOOL_FAILED
        return ReasonCode.UNEXPECTED_ERROR
