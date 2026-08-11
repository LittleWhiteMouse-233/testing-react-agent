from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    filter_messages,
    trim_messages,
)
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import RetryPolicy, TimeoutPolicy
from pydantic import BaseModel, ConfigDict, Field

from app.device.contracts import DeviceProvider
from app.domain.activity import activity_for_task
from app.domain.errors import (
    ActionTimeout,
    CaptureFailed,
    DeviceUnavailable,
    ModelCallTimeout,
    ReasonCode,
)
from app.domain.execution import (
    TaskAgentCompletion,
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
)
from app.domain.ids import ArtifactId
from app.domain.planning import TestTask, TestTaskType
from app.graph.signals import MessageValidationSignal, ToolStartedSignal
from app.llm.contracts import ChatModelProvider
from app.llm.registry import ModelRegistry
from app.services.artifacts import ArtifactStore
from app.services.registry import RunRegistry
from app.tools import ToolProvider


TaskRoute = Literal["guard", "capture", "model", "validate", "tools", "end"]
_FRAMEWORK_DEFAULT_RETRY = RetryPolicy().retry_on


def _retry_model_error(exc: Exception) -> bool:
    """Extend the framework default for asyncio model-call timeouts."""

    if isinstance(exc, TimeoutError):
        return True
    if isinstance(_FRAMEWORK_DEFAULT_RETRY, type):
        return isinstance(exc, _FRAMEWORK_DEFAULT_RETRY)
    if callable(_FRAMEWORK_DEFAULT_RETRY):
        return _FRAMEWORK_DEFAULT_RETRY(exc)
    return isinstance(exc, tuple(_FRAMEWORK_DEFAULT_RETRY))


class TaskAgentGraphState(MessagesState):
    cycle_count: int
    model_attempt: int
    completion: TaskAgentCompletion | None
    route: TaskRoute


class FinishTaskRunToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    summary: str = Field(min_length=1)


@tool(args_schema=FinishTaskRunToolInput)
async def finish_task(status: str, summary: str) -> str:
    """Finish the current task with an evidence-based terminal decision."""

    return f"{status}: {summary}"


def _prompt(task_type: TestTaskType) -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / f"{task_type.value}.txt"
    return path.read_text("utf-8")


class TaskAgentFactory:
    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        model_registry: ModelRegistry,
        tool_provider: ToolProvider,
        registry: RunRegistry,
        devices: dict[str, DeviceProvider],
        history_max_tokens: int = 8_000,
        action_timeout_seconds: float = 15,
        capture_max_attempts: int = 3,
        model_call_max_attempts: int = 3,
        model_response_max_attempts: int = 3,
    ) -> None:
        self.artifacts = artifacts
        self.model_registry = model_registry
        self.tool_provider = tool_provider
        self.registry = registry
        self.devices = devices
        self.history_max_tokens = history_max_tokens
        self.action_timeout_seconds = action_timeout_seconds
        self.capture_max_attempts = capture_max_attempts
        self.model_call_max_attempts = model_call_max_attempts
        self.model_response_max_attempts = model_response_max_attempts

    async def build(
        self,
        *,
        test_run_id: str,
        task_run_id: str,
        device_id: str,
        task: TestTask,
        previous_task_runs: list[TaskRun],
        checkpointer: BaseCheckpointSaver[Any] | None,
    ) -> tuple[Any, TaskAgentGraphState]:
        device = self.devices.get(device_id)
        if device is None:
            raise LookupError(f"Device is not registered: {device_id}")
        activity = activity_for_task(task.definition.type)
        tools = self.tool_provider.tools_for(device_id, activity)
        provider = self.model_registry.for_activity(activity)
        runtime = _TaskRuntime(
            artifacts=self.artifacts,
            registry=self.registry,
            test_run_id=test_run_id,
            task_run_id=task_run_id,
            task=task,
            previous_task_runs=previous_task_runs,
            device=device,
            tools=tools,
            provider=provider,
            prompt_text=_prompt(task.definition.type),
            history_max_tokens=self.history_max_tokens,
            model_response_max_attempts=self.model_response_max_attempts,
        )
        all_tools = (*tools, finish_task)
        tool_node = ToolNode(all_tools, handle_tool_errors=True)

        graph = StateGraph(TaskAgentGraphState)
        graph.add_node("initialize", runtime.initialize)
        graph.add_node("guard", runtime.guard)
        graph.add_node(
            "capture",
            runtime.capture,
            retry_policy=RetryPolicy(
                initial_interval=0,
                backoff_factor=1,
                max_interval=0,
                max_attempts=self.capture_max_attempts,
                jitter=False,
                retry_on=(CaptureFailed, DeviceUnavailable, ActionTimeout),
            ),
        )
        graph.add_node(
            "model",
            runtime.call_model,
            retry_policy=RetryPolicy(
                initial_interval=0.25,
                backoff_factor=2,
                max_interval=1,
                max_attempts=self.model_call_max_attempts,
                jitter=False,
                retry_on=_retry_model_error,
            ),
        )
        graph.add_node("validate", runtime.validate_message)
        graph.add_node("announce_tool", runtime.announce_tool)
        graph.add_node(
            "tools",
            tool_node,
            timeout=TimeoutPolicy(run_timeout=self.action_timeout_seconds),
        )
        graph.add_node("after_tools", runtime.after_tools)
        graph.add_edge(START, "initialize")
        graph.add_edge("initialize", "guard")
        graph.add_conditional_edges(
            "guard", lambda state: state["route"], {"capture": "capture", "end": END}
        )
        graph.add_edge("capture", "model")
        graph.add_edge("model", "validate")
        graph.add_conditional_edges(
            "validate",
            lambda state: state["route"],
            {"tools": "announce_tool", "model": "model", "end": END},
        )
        graph.add_conditional_edges(
            "announce_tool",
            lambda state: state["route"],
            {"tools": "tools", "end": END},
        )
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges(
            "after_tools",
            lambda state: state["route"],
            {"guard": "guard", "model": "model", "end": END},
        )
        compiled = graph.compile(checkpointer=checkpointer)
        initial: TaskAgentGraphState = {
            "messages": [],
            "cycle_count": 0,
            "model_attempt": 0,
            "completion": None,
            "route": "guard",
        }
        return compiled, initial


@dataclass
class _TaskRuntime:
    artifacts: ArtifactStore
    registry: RunRegistry
    test_run_id: str
    task_run_id: str
    task: TestTask
    previous_task_runs: list[TaskRun]
    device: DeviceProvider
    tools: tuple[BaseTool, ...]
    provider: ChatModelProvider
    prompt_text: str
    history_max_tokens: int
    model_response_max_attempts: int
    bound_model: Any = field(default=None, init=False)

    async def initialize(self, state: TaskAgentGraphState) -> dict[str, object]:
        context = {
            "task": self.task.model_dump(mode="json"),
            "previous_task_runs": [
                item.model_dump(mode="json") for item in self.previous_task_runs
            ],
        }
        return {
            "messages": [
                SystemMessage(content=self.prompt_text),
                HumanMessage(content=json.dumps(context, ensure_ascii=False)),
            ],
            "route": "guard",
        }

    async def guard(self, state: TaskAgentGraphState) -> dict[str, object]:
        cycle_count = state.get("cycle_count", 0)
        if self.registry.cancellation(self.test_run_id).is_set():
            return self._completion(
                TaskRunStatus.SKIPPED,
                ReasonCode.USER_CANCELLED,
                "Cancelled by user",
                cycle_count,
            )
        if cycle_count >= self.task.definition.max_cycles:
            return self._completion(
                TaskRunStatus.FAILED,
                ReasonCode.CYCLE_LIMIT,
                f"Reached max_cycles={self.task.definition.max_cycles}",
                cycle_count,
                evidence=self._latest_artifact_ids(state["messages"]),
            )
        return {
            "cycle_count": cycle_count + 1,
            "model_attempt": 0,
            "route": "capture",
        }

    async def capture(self, state: TaskAgentGraphState) -> dict[str, object]:
        screenshot = await self.device.screenshot()
        encoded_task = asyncio.to_thread(
            lambda: base64.b64encode(screenshot.content).decode("ascii")
        )
        artifact_task = self.artifacts.save_screenshot(
            task_run_id=self.task_run_id,
            content=screenshot.content,
            mime_type=screenshot.mime_type,
        )
        encoded, artifact = await asyncio.gather(encoded_task, artifact_task)
        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"Cycle {state['cycle_count']} screenshot; "
                        f"activity={screenshot.activity or 'unknown'}"
                    ),
                },
                {
                    "type": "image",
                    "base64": encoded,
                    "mime_type": screenshot.mime_type,
                    "id": artifact.id,
                },
            ]
        )
        return {"messages": [message], "route": "model"}

    async def call_model(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.bound_model is None:
            self.bound_model = self.provider.create_model().bind_tools(
                [*self.tools, finish_task], parallel_tool_calls=False
            )
        try:
            response = await asyncio.wait_for(
                self.bound_model.ainvoke(self._model_window(state["messages"])),
                timeout=self.provider.timeout_seconds,
            )
        except TimeoutError as exc:
            raise ModelCallTimeout("Model call timed out") from exc
        if not isinstance(response, AIMessage):
            raise TypeError("chat model must return AIMessage")
        return {
            "messages": [response],
            "model_attempt": state.get("model_attempt", 0) + 1,
            "route": "validate",
        }

    async def validate_message(
        self, state: TaskAgentGraphState
    ) -> dict[str, object]:
        message = state["messages"][-1]
        if not isinstance(message, AIMessage):
            raise TypeError("validation node requires the latest AIMessage")
        reason: str | None = None
        if len(message.tool_calls) != 1:
            reason = "response must contain exactly one tool call"
        else:
            call = message.tool_calls[0]
            if not call.get("id"):
                reason = "tool call requires a non-empty id"
            elif call.get("name") not in {tool.name for tool in self.tools} | {
                finish_task.name
            }:
                reason = f"tool is not available: {call.get('name')}"
            elif call.get("name") == finish_task.name:
                try:
                    FinishTaskRunToolInput.model_validate(call.get("args") or {})
                except Exception as exc:
                    reason = f"finish_task arguments are invalid: {exc}"
        if reason is None:
            return {"route": "tools"}
        if message.id is None:
            raise ValueError("LangGraph add_messages did not assign a message id")
        get_stream_writer()(
            MessageValidationSignal(
                message_id=message.id,
                attempt=state["model_attempt"],
                reason=reason,
            ).model_dump(mode="json")
        )
        if state["model_attempt"] >= self.model_response_max_attempts:
            return self._completion(
                TaskRunStatus.BLOCKED,
                ReasonCode.INVALID_MODEL_RESPONSE,
                reason,
                state["cycle_count"],
            )
        repair_messages: list[BaseMessage] = []
        for call in message.tool_calls:
            call_id = call.get("id")
            if call_id:
                repair_messages.append(
                    ToolMessage(
                        content=reason,
                        tool_call_id=call_id,
                        name=call.get("name"),
                        status="error",
                    )
                )
        repair_messages.append(
            HumanMessage(
                content=(
                    "The previous response was invalid. Return exactly one allowed "
                    f"tool call with a new id. Error: {reason}"
                )
            )
        )
        return {"messages": repair_messages, "route": "model"}

    async def announce_tool(
        self, state: TaskAgentGraphState
    ) -> dict[str, object]:
        call = self._latest_ai_call(state["messages"])
        if self.registry.cancellation(self.test_run_id).is_set():
            return {
                "messages": [
                    ToolMessage(
                        content="Cancelled before tool execution",
                        tool_call_id=call["id"],
                        name=call["name"],
                        status="error",
                    )
                ],
                **self._completion(
                    TaskRunStatus.SKIPPED,
                    ReasonCode.USER_CANCELLED,
                    "Cancelled before tool execution",
                    state["cycle_count"],
                ),
            }
        get_stream_writer()(
            ToolStartedSignal(call_id=call["id"]).model_dump(mode="json")
        )
        return {"route": "tools"}

    async def after_tools(self, state: TaskAgentGraphState) -> dict[str, object]:
        tool_message = state["messages"][-1]
        if not isinstance(tool_message, ToolMessage):
            raise TypeError("ToolNode must append a ToolMessage")
        call = self._latest_ai_call(state["messages"][:-1])
        if call["name"] == finish_task.name:
            terminal = FinishTaskRunToolInput.model_validate(call.get("args") or {})
            status = TaskRunStatus(terminal.status)
            reason = {
                TaskRunStatus.PASSED: ReasonCode.COMPLETED,
                TaskRunStatus.FAILED: (
                    ReasonCode.GOAL_UNREACHABLE
                    if self.task.definition.type == TestTaskType.ACT
                    else ReasonCode.ASSERTION_FAILED
                ),
                TaskRunStatus.BLOCKED: ReasonCode.AGENT_BLOCKED,
            }[status]
            evidence = (
                self._latest_artifact_ids(state["messages"])
                if status in {TaskRunStatus.PASSED, TaskRunStatus.FAILED}
                else []
            )
            return self._completion(
                status,
                reason,
                terminal.summary,
                state["cycle_count"],
                evidence=evidence,
            )
        if tool_message.status != "error":
            return {"route": "guard"}
        error_text = tool_message.text or "Tool execution failed"
        if state["model_attempt"] >= self.model_response_max_attempts:
            return self._completion(
                TaskRunStatus.BLOCKED,
                ReasonCode.TOOL_FAILED,
                error_text,
                state["cycle_count"],
            )
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "The tool failed. Use the existing screenshot and tool result "
                        "to select one safe alternative call, or finish as blocked."
                    )
                )
            ],
            "route": "model",
        }

    def _model_window(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        image_message_ids = [
            message.id
            for message in messages
            if message.id is not None and self._message_has_image(message)
        ]
        selected = filter_messages(
            list(messages),
            exclude_ids=image_message_ids[:-1],
        )
        return trim_messages(
            selected,
            max_tokens=self.history_max_tokens,
            token_counter=self._estimated_tokens,
            strategy="last",
            allow_partial=False,
            include_system=True,
            start_on="human",
        )

    @staticmethod
    def _estimated_tokens(messages: list[BaseMessage]) -> int:
        total = 0
        for message in messages:
            for block in message.content_blocks:
                if block.get("type") == "image":
                    total += 256
                else:
                    total += max(1, len(json.dumps(block, ensure_ascii=False)) // 4)
            total += len(getattr(message, "tool_calls", [])) * 64
        return total

    @staticmethod
    def _message_has_image(message: BaseMessage) -> bool:
        return any(block.get("type") == "image" for block in message.content_blocks)

    @staticmethod
    def _latest_ai_call(messages: Sequence[BaseMessage]) -> dict[str, Any]:
        for message in reversed(messages):
            if isinstance(message, AIMessage) and len(message.tool_calls) == 1:
                return dict(message.tool_calls[0])
        raise ValueError("No single AI tool call is available")

    @staticmethod
    def _latest_artifact_ids(
        messages: Sequence[BaseMessage],
    ) -> list[ArtifactId]:
        for message in reversed(messages):
            ids: list[ArtifactId] = []
            for block in message.content_blocks:
                value = block.get("id")
                if block.get("type") == "image" and isinstance(value, str):
                    ids.append(cast(ArtifactId, value))
            if ids:
                return ids
        return []

    @staticmethod
    def _completion(
        status: TaskRunStatus,
        reason_code: ReasonCode,
        summary: str,
        cycle_count: int,
        *,
        evidence: list[ArtifactId] | None = None,
    ) -> dict[str, object]:
        completion = TaskAgentCompletion(
            status=status,
            result=TaskRunResult(
                reason_code=reason_code,
                summary=summary,
                evidence_artifact_ids=evidence or [],
            ),
            cycle_count=cycle_count,
        )
        return {"completion": completion, "route": "end"}
