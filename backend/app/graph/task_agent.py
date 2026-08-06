from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.types import Command, RetryPolicy, TimeoutPolicy
from pydantic import BaseModel, ConfigDict, Field

from app.device.contracts import DeviceProvider
from app.domain.activity import Activity
from app.domain.errors import ReasonCode
from app.domain.events import (
    AgentActionSelectedEvent,
    AgentResponseInvalidEvent,
    AgentTerminalSelectedEvent,
    CycleStartedEvent,
    ExecutionErrorEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from app.domain.execution import ObservationRef, TaskOutcome, TaskStatus
from app.domain.planning import Task, TaskType
from app.domain.tools import (
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolInvocation,
)
from app.execution.ports import ArtifactRepository, ExecutionJournal, ExecutionRepository
from app.llm.registry import ModelRegistry
from app.services.registry import RunRegistry
from app.tools import ToolProvider


class TaskAgentState(MessagesState):
    model_id: str
    cycle_count: int
    model_attempt: int
    latest_observation: dict[str, Any] | None
    pending_invocation: dict[str, Any] | None
    terminal_outcome: dict[str, Any] | None
    route: str


@dataclass(frozen=True)
class TaskAgentPolicy:
    policy_id: str
    prompt_name: str


ACT_POLICY = TaskAgentPolicy(policy_id="act", prompt_name="act.txt")
JUDGE_POLICY = TaskAgentPolicy(policy_id="judge", prompt_name="judge.txt")


def policy_for(task: Task) -> TaskAgentPolicy:
    return ACT_POLICY if task.type == TaskType.ACT else JUDGE_POLICY


def _prompt(name: str) -> str:
    return (
        Path(__file__).resolve().parents[1] / "prompts" / name
    ).read_text("utf-8")


class FinishTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "blocked"]
    summary: str = Field(min_length=1)


@tool(args_schema=FinishTaskArgs)
async def finish_task(status: str, summary: str) -> str:
    """Finish the current task with an evidence-based terminal decision."""
    return f"{status}: {summary}"


class TaskAgentFactory:
    def __init__(
        self,
        *,
        repository: ExecutionRepository,
        journal: ExecutionJournal,
        artifacts: ArtifactRepository,
        model_registry: ModelRegistry,
        tool_provider: ToolProvider,
        registry: RunRegistry,
        devices: dict[str, DeviceProvider],
        history_max_tokens: int = 8_000,
        action_timeout_seconds: float = 15,
        tool_timeout_max_attempts: int = 3,
        tool_call_max_attempts: int = 3,
    ) -> None:
        self.repository = repository
        self.journal = journal
        self.artifacts = artifacts
        self.model_registry = model_registry
        self.tool_provider = tool_provider
        self.registry = registry
        self.devices = devices
        self.history_max_tokens = history_max_tokens
        self.action_timeout_seconds = action_timeout_seconds
        self.tool_timeout_max_attempts = tool_timeout_max_attempts
        self.tool_call_max_attempts = tool_call_max_attempts

    async def build(
        self,
        *,
        run_id: str,
        task_run_id: str,
        device_id: str,
        task: Task,
        cross_task_context: list[dict[str, Any]],
        checkpointer: BaseCheckpointSaver | None,
    ) -> Any:
        device = self.devices.get(device_id)
        if device is None:
            raise LookupError(f"Device is not registered: {device_id}")
        policy = policy_for(task)
        tool_set = self.tool_provider.tools_for(
            device_id,
            Activity(task.type.value),
        )
        runtime = _TaskRuntime(
            repository=self.repository,
            journal=self.journal,
            artifacts=self.artifacts,
            model_registry=self.model_registry,
            registry=self.registry,
            run_id=run_id,
            task_run_id=task_run_id,
            task=task,
            policy=policy,
            cross_task_context=cross_task_context,
            device=device,
            tools=tool_set.tools,
            tool_names=tool_set.names,
            prompt_text=_prompt(policy.prompt_name),
            history_max_tokens=self.history_max_tokens,
            tool_call_max_attempts=self.tool_call_max_attempts,
        )
        tool_node = ToolNode(
            tool_set.tools,
            handle_tool_errors=False,
        )

        graph = StateGraph(TaskAgentState)
        graph.add_node("guard_and_observe", runtime.guard_and_observe)
        graph.add_node("call_model", runtime.call_model)
        graph.add_node("repair_tool_call", runtime.repair_tool_call)
        graph.add_node("before_tools", runtime.before_tools)
        graph.add_node(
            "tools",
            tool_node,
            timeout=TimeoutPolicy(run_timeout=self.action_timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=0,
                backoff_factor=1,
                max_interval=0,
                max_attempts=self.tool_timeout_max_attempts,
                jitter=False,
                retry_on=NodeTimeoutError,
            ),
            # LangGraph injects NodeError into handlers at runtime, but its
            # StateNode static type does not yet describe that extra parameter.
            error_handler=cast(Any, runtime.handle_tool_error),
        )
        graph.add_node("after_tools", runtime.after_tools)
        graph.add_edge(START, "guard_and_observe")
        graph.add_conditional_edges(
            "guard_and_observe",
            lambda state: state["route"],
            {"model": "call_model", "end": END},
        )
        graph.add_conditional_edges(
            "call_model",
            lambda state: state["route"],
            {"tools": "before_tools", "repair": "repair_tool_call", "end": END},
        )
        graph.add_conditional_edges(
            "repair_tool_call",
            lambda state: state["route"],
            {"tools": "before_tools", "repair": "repair_tool_call", "end": END},
        )
        graph.add_conditional_edges(
            "before_tools",
            lambda state: state["route"],
            {"tools": "tools", "end": END},
        )
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges(
            "after_tools",
            lambda state: state["route"],
            {"observe": "guard_and_observe", "repair": "repair_tool_call", "end": END},
        )
        return graph.compile(checkpointer=checkpointer)


@dataclass
class _TaskRuntime:
    repository: ExecutionRepository
    journal: ExecutionJournal
    artifacts: ArtifactRepository
    model_registry: ModelRegistry
    registry: RunRegistry
    run_id: str
    task_run_id: str
    task: Task
    policy: TaskAgentPolicy
    cross_task_context: list[dict[str, Any]]
    device: DeviceProvider
    tools: tuple[BaseTool, ...]
    tool_names: frozenset[str]
    prompt_text: str
    history_max_tokens: int
    tool_call_max_attempts: int
    bound_models: dict[str, Any] = field(default_factory=dict)

    async def guard_and_observe(self, state: TaskAgentState) -> dict[str, Any]:
        if self.registry.cancellation(self.run_id).is_set():
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.SKIPPED,
                    reason_code=ReasonCode.USER_CANCELLED,
                    summary="Cancelled by user",
                    cycle_count=state.get("cycle_count", 0),
                )
            )
        cycle_count = state.get("cycle_count", 0)
        if cycle_count >= self.task.max_cycles:
            latest = state.get("latest_observation")
            evidence = [latest["artifact_id"]] if latest else []
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.FAILED,
                    reason_code=ReasonCode.CYCLE_LIMIT,
                    summary=f"Reached max_cycles={self.task.max_cycles}",
                    cycle_count=cycle_count,
                    evidence_artifact_ids=evidence,
                )
            )
        cycle_count += 1
        await self.repository.update_cycle(
            self.task_run_id,
            cycle_count,
            CycleStartedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=cycle_count,
            ),
        )
        screenshot = None
        last_error: Exception | None = None
        for _ in range(3):
            try:
                screenshot = await self.device.screenshot()
                break
            except Exception as exc:
                last_error = exc
        if screenshot is None:
            await self.journal.append(
                ExecutionErrorEvent(
                    run_id=self.run_id,
                    task_run_id=self.task_run_id,
                    reason_code=ReasonCode.CAPTURE_FAILED,
                    message=str(last_error),
                ),
                dedup_key=self._dedup(cycle_count, "capture_failed"),
            )
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.CAPTURE_FAILED,
                    summary=f"Screenshot failed: {last_error}",
                    cycle_count=cycle_count,
                )
            )
        artifact = await self.artifacts.save_observation(
            run_id=self.run_id,
            task_run_id=self.task_run_id,
            content=screenshot.content,
            mime_type=screenshot.mime_type,
            activity=screenshot.activity,
            cycle_count=cycle_count,
        )
        observation = ObservationRef(
            artifact_id=artifact.id,
            mime_type=screenshot.mime_type,
            activity=screenshot.activity,
            cycle_count=cycle_count,
        )
        return {
            "route": "model",
            "cycle_count": cycle_count,
            "model_attempt": 0,
            "latest_observation": observation.model_dump(mode="json"),
            "pending_invocation": None,
            "messages": [
                HumanMessage(
                    content=(
                        f"Cycle {cycle_count}: captured observation "
                        f"artifact_id={artifact.id}, activity={screenshot.activity}."
                    )
                )
            ],
        }

    async def call_model(self, state: TaskAgentState) -> dict[str, Any]:
        return await self._call_model(state, include_observation=True)

    async def repair_tool_call(self, state: TaskAgentState) -> dict[str, Any]:
        return await self._call_model(state, include_observation=False)

    async def _call_model(
        self,
        state: TaskAgentState,
        *,
        include_observation: bool,
    ) -> dict[str, Any]:
        attempt = state.get("model_attempt", 0) + 1
        observation = ObservationRef.model_validate(state["latest_observation"])
        history = trim_messages(
            state.get("messages", []),
            max_tokens=self.history_max_tokens,
            token_counter="approximate",
            strategy="last",
            allow_partial=False,
            start_on=(HumanMessage, AIMessage),
        )
        context = {
            "task": self.task.model_dump(mode="json"),
            "policy": self.policy.policy_id,
            "cycle_count": state["cycle_count"],
            "max_cycles": self.task.max_cycles,
            "recent_cross_task_context": self.cross_task_context,
            "latest_artifact_id": observation.artifact_id,
        }
        messages = [
            SystemMessage(content=self.prompt_text),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
            *history,
        ]
        if include_observation:
            raw, mime_type = await self.artifacts.load_content(observation.artifact_id)
            encoded = base64.b64encode(raw).decode("ascii")
            messages.append(
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": (
                                "Use this latest screenshot for the current decision. "
                                f"artifact_id={observation.artifact_id}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{encoded}"
                            },
                        },
                    ]
                )
            )
        model_id = state["model_id"]
        provider = self.model_registry.get(model_id)
        bound_model = self.bound_models.get(model_id)
        if bound_model is None:
            bound_model = provider.create_model().bind_tools(
                [*self.tools, finish_task], parallel_tool_calls=False
            )
            self.bound_models[model_id] = bound_model
        try:
            response = await asyncio.wait_for(
                bound_model.ainvoke(messages), timeout=provider.timeout_seconds
            )
        except Exception as exc:
            return await self._invalid_response(
                state, attempt, f"Model call failed: {exc}"
            )
        if not isinstance(response, AIMessage):
            return await self._invalid_response(
                state, attempt, "Model response is not an AIMessage"
            )
        if len(response.tool_calls) != 1:
            return await self._invalid_response(
                state, attempt, "Response must contain exactly one tool call"
            )
        call = response.tool_calls[0]
        call_id = call.get("id")
        if not call_id:
            return await self._invalid_response(
                state, attempt, "Tool call requires a non-empty id"
            )
        pending = state.get("pending_invocation")
        if pending is not None and call_id == pending.get("call_id"):
            return await self._invalid_response(
                state,
                attempt,
                "A repaired tool call must use a new tool call id",
            )
        if call["name"] == finish_task.name:
            try:
                terminal = FinishTaskArgs.model_validate(call.get("args") or {})
            except Exception as exc:
                return await self._invalid_response(
                    state, attempt, f"finish_task arguments are invalid: {exc}"
                )
            evidence = (
                [observation.artifact_id]
                if terminal.status in {"passed", "failed"}
                else []
            )
            await self.journal.append(
                AgentTerminalSelectedEvent(
                    run_id=self.run_id,
                    task_run_id=self.task_run_id,
                    cycle_count=state["cycle_count"],
                    status=terminal.status,
                    summary=terminal.summary,
                    evidence_artifact_ids=evidence,
                ),
                dedup_key=self._dedup(state["cycle_count"], "terminal"),
            )
            reason = {
                "passed": ReasonCode.COMPLETED,
                "failed": (
                    ReasonCode.GOAL_UNREACHABLE
                    if self.task.type == TaskType.ACT
                    else ReasonCode.ASSERTION_FAILED
                ),
                "blocked": ReasonCode.AGENT_BLOCKED,
            }[terminal.status]
            update = self._outcome_update(
                TaskOutcome(
                    status=TaskStatus(terminal.status),
                    reason_code=reason,
                    summary=terminal.summary,
                    cycle_count=state["cycle_count"],
                    evidence_artifact_ids=evidence,
                )
            )
            update["messages"] = [response]
            return update
        if call["name"] not in self.tool_names:
            return await self._invalid_response(
                state, attempt, f"Tool is not available: {call['name']}"
            )
        summary = response.text.strip()
        if not summary:
            return await self._invalid_response(
                state, attempt, "Tool call requires an explanation in message content"
            )
        invocation = ToolInvocation(
            call_id=call_id,
            name=call["name"],
            arguments=dict(call.get("args") or {}),
            decision_summary=summary,
        )
        await self.journal.append(
            AgentActionSelectedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=state["cycle_count"],
                invocation=invocation,
            ),
            dedup_key=f"{self.run_id}:{self.task_run_id}:{call_id}:selected",
        )
        return {
            "route": "tools",
            "model_attempt": attempt,
            "pending_invocation": invocation.model_dump(mode="json"),
            "messages": [response],
        }

    async def before_tools(self, state: TaskAgentState) -> dict[str, Any]:
        invocation = ToolInvocation.model_validate(state["pending_invocation"])
        await self.journal.append(
            ToolStartedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=state["cycle_count"],
                invocation=invocation,
            ),
            dedup_key=self._dedup(
                state["cycle_count"], f"tool.started:{invocation.call_id}"
            ),
        )
        if not self.registry.cancellation(self.run_id).is_set():
            return {"route": "tools"}
        result = ToolExecutionResult(
            status=ToolExecutionStatus.CANCELLED,
            summary="Cancelled before tool execution",
        )
        await self._record_tool_finished(state, invocation, result)
        return self._outcome_update(
            TaskOutcome(
                status=TaskStatus.SKIPPED,
                reason_code=ReasonCode.USER_CANCELLED,
                summary=result.summary,
                cycle_count=state["cycle_count"],
            )
        )

    async def handle_tool_error(
        self,
        state: TaskAgentState,
        error: NodeError,
    ) -> Command[Literal["after_tools"]]:
        invocation = ToolInvocation.model_validate(state["pending_invocation"])
        if isinstance(error.error, NodeTimeoutError):
            status = ToolExecutionStatus.TIMED_OUT
            fallback = f"Tool {invocation.name} timed out"
        elif isinstance(error.error, ToolInvocationError):
            status = ToolExecutionStatus.INVALID
            fallback = f"Tool {invocation.name} arguments are invalid"
        else:
            status = ToolExecutionStatus.BLOCKED
            fallback = f"Tool {invocation.name} failed"
        result = ToolExecutionResult(
            status=status,
            summary=str(error.error) or fallback,
        )
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=result.summary,
                        artifact=result.model_dump(mode="json"),
                        name=invocation.name,
                        tool_call_id=invocation.call_id,
                        status="error",
                    )
                ]
            },
            goto="after_tools",
        )

    async def after_tools(self, state: TaskAgentState) -> dict[str, Any]:
        last = state.get("messages", [])[-1]
        if not isinstance(last, ToolMessage):
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.TOOL_FAILED,
                    summary="ToolNode did not return a ToolMessage",
                    cycle_count=state["cycle_count"],
                )
            )
        invocation = ToolInvocation.model_validate(state["pending_invocation"])
        if last.status == "error":
            try:
                result = ToolExecutionResult.model_validate(last.artifact)
            except Exception as exc:
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.BLOCKED,
                    summary=f"Tool error result is invalid: {exc}",
                )
        else:
            result = ToolExecutionResult(
                status=ToolExecutionStatus.SUCCEEDED,
                summary=self._content_text(last.content),
                data=self._result_data(last),
            )
        await self._record_tool_finished(state, invocation, result)
        if result.status == ToolExecutionStatus.CANCELLED:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.SKIPPED,
                    reason_code=ReasonCode.USER_CANCELLED,
                    summary=result.summary,
                    cycle_count=state["cycle_count"],
                )
            )
        if result.status == ToolExecutionStatus.INVALID:
            return await self._invalid_response(
                state, state.get("model_attempt", 0), result.summary
            )
        if result.status in {
            ToolExecutionStatus.BLOCKED,
            ToolExecutionStatus.TIMED_OUT,
        }:
            return self._tool_failure(state, result)
        return {
            "route": "observe",
            "pending_invocation": None,
        }

    def _tool_failure(
        self,
        state: TaskAgentState,
        result: ToolExecutionResult,
    ) -> dict[str, Any]:
        attempt = state.get("model_attempt", 0)
        if attempt >= self.tool_call_max_attempts:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.TOOL_FAILED,
                    summary=(
                        "Tool calls failed after "
                        f"{self.tool_call_max_attempts} model attempts: {result.summary}"
                    ),
                    cycle_count=state["cycle_count"],
                )
            )
        return {
            "route": "repair",
            "messages": [
                HumanMessage(
                    content=(
                        "The tool call failed without changing the observation. "
                        "Use the error and the existing decision context to return "
                        "exactly one new allowed tool call, or finish as blocked if "
                        f"no safe alternative exists. Error: {result.summary}"
                    )
                )
            ],
        }

    async def _record_tool_finished(
        self,
        state: TaskAgentState,
        invocation: ToolInvocation,
        result: ToolExecutionResult,
    ) -> None:
        await self.journal.append(
            ToolFinishedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=state["cycle_count"],
                invocation=invocation,
                result=result,
            ),
            dedup_key=self._dedup(
                state["cycle_count"], f"tool.finished:{invocation.call_id}"
            ),
        )

    async def _invalid_response(
        self, state: TaskAgentState, attempt: int, message: str
    ) -> dict[str, Any]:
        await self.journal.append(
            AgentResponseInvalidEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=state["cycle_count"],
                attempt=attempt,
                message=message,
            ),
            dedup_key=self._dedup(
                state["cycle_count"], f"invalid_response:{attempt}"
            ),
        )
        if attempt >= self.tool_call_max_attempts:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.INVALID_MODEL_RESPONSE,
                    summary=(
                        "Invalid model response after "
                        f"{self.tool_call_max_attempts} attempts: {message}"
                    ),
                    cycle_count=state["cycle_count"],
                )
            )
        return {
            "route": "repair",
            "model_attempt": attempt,
            "messages": [
                HumanMessage(
                    content=(
                        "The previous response was invalid. Return exactly one "
                        f"allowed tool call. Error: {message}"
                    )
                )
            ],
        }

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content or "Tool completed"
        return json.dumps(content, ensure_ascii=False, default=str)

    @staticmethod
    def _result_data(message: ToolMessage) -> dict[str, Any]:
        value = message.artifact if message.artifact is not None else message.content
        if isinstance(value, dict):
            return value
        try:
            serialized = json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            serialized = str(value)
        return {"output": serialized}

    @staticmethod
    def _outcome_update(outcome: TaskOutcome) -> dict[str, Any]:
        return {
            "route": "end",
            "terminal_outcome": outcome.model_dump(mode="json"),
        }

    def _dedup(self, cycle: int, suffix: str) -> str:
        return f"{self.run_id}:{self.task_run_id}:{cycle}:{suffix}"
