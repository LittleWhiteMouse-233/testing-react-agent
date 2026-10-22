from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, MessagesState, StateGraph, add_messages
from langgraph.graph.message import Messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, RetryPolicy
# Documented public API; LangGraph 1.2.9 omits this re-export from its type declarations.
from langgraph.types import default_retry_on  # type: ignore[reportPrivateImportUsage]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from app.domain.errors import (
    ToolCallError,
    ModelContextBudgetExceeded,
    ModelProfileMismatch,
    ModelCallTimeout,
    PromptVersionMismatch,
)
from app.domain.execution import (
    TaskAgentCompletion,
    ReasonCode,
    TaskRun,
    TaskRunResult,
    TaskRunStatus,
)
from app.domain.resources.llm import LLMProfileSnapshot
from app.domain.resources.static_resources import ResourceCatalogSnapshot
from app.skill import top_level_skills
from app.domain.ids import TaskRunId
from app.domain.planning import TestTask, TestTaskType
from app.execution.model_context import ModelContextWindow
from app.execution.task_agent_events import TaskAgentEventRecorder
from app.artifacts import ArtifactStore
from app.llm import ChatModelClient, ModelProvider
from app.prompts import PromptDefinition


_JSON_VALUE = TypeAdapter(JsonValue)


class TaskAgentGraphState(MessagesState):
    cycle_count: int
    model_response_attempt: int
    completion: TaskAgentCompletion | None
    route: Literal["cycle_guard", "model", "tools", "end"]


class FinishTaskRunToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    summary: str = Field(min_length=1)


@tool(args_schema=FinishTaskRunToolInput)
async def finish_task(status: str, summary: str) -> str:
    """Finish this task with passed, failed or blocked status.

    For every status, summary must describe the observations and reasoning supporting
    the selected conclusion, and how they relate to this task's success criteria.
    """

    return f"{status}: {summary}"


@dataclass(frozen=True)
class TaskAgentInput:
    """Agent input projected by the executor from persisted task/environment facts.

    This task-local input is not persisted; the plan, run environment, and
    completed TaskRuns remain authoritative. Live dependencies stay separate.
    """

    task_run_id: TaskRunId
    task: TestTask
    previous_task_runs: tuple[TaskRun, ...]
    max_cycles: int
    image_history_rounds: int
    expected_model_profile: LLMProfileSnapshot
    expected_prompt_version: str
    resource_catalog: ResourceCatalogSnapshot


class TaskAgentFactory:
    def __init__(
        self, *, artifacts: ArtifactStore, model_provider: ModelProvider,
        act_prompt: PromptDefinition, judge_prompt: PromptDefinition,
        tool_call_timeout_seconds: float = 120,
        model_call_max_attempts: int = 3, model_response_max_attempts: int = 3,
    ) -> None:
        self.artifacts = artifacts
        self.model_provider = model_provider
        self.prompts_by_task_type = {TestTaskType.ACT: act_prompt, TestTaskType.JUDGE: judge_prompt}
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self.model_call_max_attempts = model_call_max_attempts
        self.model_response_max_attempts = model_response_max_attempts

    async def build(
        self, *, task_input: TaskAgentInput, tools: tuple[BaseTool, ...],
        run_cancellation_event: asyncio.Event, checkpointer: BaseCheckpointSaver[Any] | None,
        event_recorder: TaskAgentEventRecorder,
    ) -> tuple[Any, TaskAgentGraphState]:
        try:
            model_client = self.model_provider.client_by_id(task_input.expected_model_profile.profile_id)
        except LookupError as exc:
            raise ModelProfileMismatch(str(exc)) from exc
        if model_client.profile_snapshot != task_input.expected_model_profile:
            raise ModelProfileMismatch("Resolved model does not match the TestRun environment snapshot")
        prompt = self.prompts_by_task_type[task_input.task.definition.type]
        if prompt.version != task_input.expected_prompt_version:
            raise PromptVersionMismatch(expected=task_input.expected_prompt_version, actual=prompt.version, prompt_name=prompt.name)
        model_tools: tuple[BaseTool, ...] = (*tools, finish_task)
        runtime = _TaskRuntime(
            artifacts=self.artifacts, run_cancellation_event=run_cancellation_event,
            task_input=task_input,
            tools=model_tools, model_client=model_client, prompt_definition=prompt,
            model_context_window=ModelContextWindow(model_profile=task_input.expected_model_profile, tools=model_tools),
            model_response_max_attempts=self.model_response_max_attempts,
            event_recorder=event_recorder,
        )
        graph = StateGraph(TaskAgentGraphState)
        graph.add_node("initialize", runtime.initialize)
        graph.add_node("cycle_guard", runtime.cycle_guard)
        graph.add_node("model", runtime.call_model, retry_policy=RetryPolicy(
            initial_interval=0.25, backoff_factor=2, max_interval=1,
            max_attempts=self.model_call_max_attempts, jitter=False, retry_on=_retry_model_error,
        ))
        graph.add_node("validate", runtime.validate_message)
        graph.add_node("announce_tool", runtime.announce_tool)
        graph.add_node(
            "tools", ToolNode(model_tools), timeout=self.tool_call_timeout_seconds,
            # LangGraph 1.2.9 supports NodeError injection but omits it from StateNode's type.
            error_handler=runtime.handle_tool_error,  # type: ignore[reportArgumentType]
        )
        graph.add_node("after_tools", runtime.after_tools)
        graph.add_edge(START, "initialize")
        graph.add_edge("initialize", "cycle_guard")
        graph.add_conditional_edges("cycle_guard", lambda state: state["route"], {"model": "model", "end": END})
        graph.add_edge("model", "validate")
        graph.add_conditional_edges("validate", lambda state: state["route"],
                                    {"tools": "announce_tool", "cycle_guard": "cycle_guard", "end": END})
        graph.add_conditional_edges("announce_tool", lambda state: state["route"], {"tools": "tools", "end": END})
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges("after_tools", lambda state: state["route"], {"cycle_guard": "cycle_guard", "end": END})
        return graph.compile(checkpointer=checkpointer), {
            "messages": [], "cycle_count": 0, "model_response_attempt": 0, "completion": None, "route": "cycle_guard",
        }


def _retry_model_error(exc: Exception) -> bool:
    """Extend the framework default for asyncio model-call timeouts."""

    if isinstance(exc, ModelContextBudgetExceeded):
        return False
    if isinstance(exc, TimeoutError):
        return True
    return default_retry_on(exc)


@dataclass
class _TaskRuntime:
    artifacts: ArtifactStore
    run_cancellation_event: asyncio.Event
    task_input: TaskAgentInput
    tools: tuple[BaseTool, ...]
    model_client: ChatModelClient
    model_context_window: ModelContextWindow
    prompt_definition: PromptDefinition
    model_response_max_attempts: int
    event_recorder: TaskAgentEventRecorder
    bound_model: Any = field(default=None, init=False)

    async def initialize(self, state: TaskAgentGraphState) -> dict[str, object]:
        return {"messages": [
            SystemMessage(content=self.prompt_definition.text),
            HumanMessage(content=json.dumps({
                "task": self.task_input.task.model_dump(mode="json"),
                "max_cycles": self.task_input.max_cycles,
                "previous_task_runs": [run.model_dump(mode="json") for run in self.task_input.previous_task_runs],
                "available_skills": [entry.model_dump(include={"source_id", "uri", "name", "description"})
                                     for entry in top_level_skills(self.task_input.resource_catalog)],
            }, ensure_ascii=False)),
        ], "route": "cycle_guard"}

    async def cycle_guard(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.run_cancellation_event.is_set():
            return self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state["cycle_count"])
        if state["cycle_count"] >= self.task_input.max_cycles:
            return self._completion(TaskRunStatus.BLOCKED, ReasonCode.CYCLE_LIMIT,
                                    "Decision round limit reached", state["cycle_count"])
        cycle_count = state["cycle_count"] + 1
        await self.event_recorder.record_cycle_started(cycle_count)
        update: dict[str, object] = {"cycle_count": cycle_count, "route": "model"}
        if self.run_cancellation_event.is_set():
            update.update(self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", cycle_count))
        return update

    async def call_model(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.bound_model is None:
            self.bound_model = self.model_client.create_model().bind_tools(self.tools, parallel_tool_calls=False)
        try:
            response = await asyncio.wait_for(self.bound_model.ainvoke(self.model_context_window.select_messages(
                state["messages"],
            )), timeout=self.model_client.timeout_seconds)
        except TimeoutError as exc:
            raise ModelCallTimeout("Model call timed out") from exc
        if not isinstance(response, AIMessage):
            raise TypeError("chat model must return AIMessage")
        return {"messages": [response], "model_response_attempt": state["model_response_attempt"] + 1}

    async def validate_message(self, state: TaskAgentGraphState) -> dict[str, object]:
        message = state["messages"][-1]
        assert isinstance(message, AIMessage)
        reason: str | None = None
        if len(message.tool_calls) != 1 or message.invalid_tool_calls:
            reason = "response must contain exactly one valid tool call"
        else:
            call = message.tool_calls[0]
            if not call.get("id"):
                reason = "tool call requires a non-empty id"
            elif call["name"] not in {tool.name for tool in self.tools}:
                reason = f"tool is not available: {call['name']}"
            elif call["name"] == finish_task.name:
                try:
                    FinishTaskRunToolInput.model_validate(call["args"])
                except ValueError as exc:
                    reason = str(exc)
        if reason is None:
            return {"route": "tools"}
        assert message.id is not None
        await self.event_recorder.record_message_validation_failed(message.id, state["model_response_attempt"], reason)
        repairs: list[BaseMessage] = [ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"], status="error")
                                    for call in message.tool_calls if call.get("id")]
        if self.run_cancellation_event.is_set():
            return {"messages": repairs,
                    **self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state["cycle_count"])}
        if state["cycle_count"] >= self.task_input.max_cycles:
            return {"messages": repairs, "route": "cycle_guard"}
        if state["model_response_attempt"] >= self.model_response_max_attempts:
            return {"messages": repairs,
                    **self._completion(TaskRunStatus.BLOCKED, ReasonCode.INVALID_MODEL_RESPONSE, reason, state["cycle_count"])}
        repairs.append(HumanMessage(content=f"Return one allowed tool call with a new id. {reason}"))
        return {"messages": repairs, "route": "cycle_guard"}

    async def announce_tool(self, state: TaskAgentGraphState) -> dict[str, object]:
        call = _latest_ai_call(state["messages"])
        if not self.run_cancellation_event.is_set():
            await self.event_recorder.record_tool_started(call["id"])
        if self.run_cancellation_event.is_set():
            return {"messages": [ToolMessage(content="Cancelled before execution", tool_call_id=call["id"], name=call["name"], status="error")],
                    **self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled before tool execution", state["cycle_count"])}
        return {"route": "tools"}

    def handle_tool_error(
        self, state: TaskAgentGraphState, error: NodeError,
    ) -> Command[Literal["after_tools"]]:
        """Return only node timeouts to the model; other failures reach the executor."""
        if not isinstance(error.error, NodeTimeoutError):
            raise error.error
        call = _latest_ai_call(state["messages"])
        return Command(update={"messages": [ToolMessage(
            content=("Tool execution timed out; the operation may have partially executed. "
                     "Unreceived results are unconfirmed. No operation was automatically replayed. "
                     "Observe the target before deciding the next action."),
            tool_call_id=call["id"], name=call["name"], status="error",
        )]}, goto="after_tools")

    async def _prepare_tool_result(self, result: ToolMessage) -> ToolMessage:
        """Save images and project content; application failures propagate."""
        content: list[dict[str, Any]] = []
        image_references: dict[str, str] = {}
        try:
            for block in result.content_blocks:
                saved_block = dict(block)
                if block.get("type") == "image":
                    encoded = block.get("base64")
                    mime_type = block.get("mime_type")
                    if not isinstance(encoded, str) or not isinstance(mime_type, str):
                        raise ValueError("Images must contain inline base64 and MIME type")
                    raw = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
                    artifact = await self.artifacts.save_image(
                        task_run_id=self.task_input.task_run_id, content=raw, mime_type=mime_type,
                    )
                    saved_block["id"] = artifact.id
                    image_references[encoded] = f"[Image artifact {artifact.id}]"
                    image_references[f"data:{mime_type};base64,{encoded}"] = image_references[encoded]
                elif block.get("type") != "text":
                    raise ValueError(f"Unsupported tool content block: {block.get('type')!r}")
                content.append(saved_block)
            # Adapter structuredContent is projected once, then removed from checkpoints.
            structured = None
            has_structured_content = isinstance(result.artifact, dict) and "structured_content" in result.artifact
            if has_structured_content:
                structured = _JSON_VALUE.validate_python(result.artifact["structured_content"])
            already_in_text = False
            for block in content:
                if block.get("type") != "text":
                    continue
                try:
                    text_value = json.loads(block["text"])
                except (ValueError, TypeError):
                    continue
                already_in_text |= text_value == structured
                projected = _project_structured_tool_content(text_value, image_references)
                if projected != text_value:
                    block["text"] = json.dumps(projected, ensure_ascii=False)
            if has_structured_content and not already_in_text:
                content.append({"type": "text", "text": json.dumps(
                    _project_structured_tool_content(structured, image_references), ensure_ascii=False,
                )})
        except Exception as exc:
            raise ToolCallError("Tool result processing failed") from exc
        return result.model_copy(update={"content": content, "artifact": None})

    async def after_tools(self, state: TaskAgentGraphState) -> dict[str, object]:
        tool_message = state["messages"][-1]
        assert isinstance(tool_message, ToolMessage)
        tool_message = await self._prepare_tool_result(tool_message)
        messages = cast(list[BaseMessage], add_messages(cast(Messages, state["messages"]), [tool_message]))
        # Product image retention policy; add_messages replaces these messages
        # by ID without mutating earlier State snapshots or persisted events.
        replacements = _collect_expired_image_updates(messages, self.task_input.image_history_rounds)
        update: dict[str, object] = {"route": "cycle_guard", "messages": [*replacements, tool_message]}
        if self.run_cancellation_event.is_set():
            update.update(self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state["cycle_count"]))
        elif (call := _latest_ai_call(state["messages"]))["name"] == finish_task.name and tool_message.status != "error":
            terminal = FinishTaskRunToolInput.model_validate(call["args"])
            status = TaskRunStatus(terminal.status)
            reason = {TaskRunStatus.PASSED: ReasonCode.COMPLETED, TaskRunStatus.BLOCKED: ReasonCode.AGENT_BLOCKED,
                      TaskRunStatus.FAILED: ReasonCode.GOAL_UNREACHABLE if self.task_input.task.definition.type == TestTaskType.ACT else ReasonCode.ASSERTION_FAILED}[status]
            update.update(self._completion(status, reason, terminal.summary, state["cycle_count"]))
        elif tool_message.status == "error":
            if (state["cycle_count"] < self.task_input.max_cycles
                    and state["model_response_attempt"] >= self.model_response_max_attempts):
                update.update(self._completion(TaskRunStatus.BLOCKED, ReasonCode.TOOL_FAILED, tool_message.text, state["cycle_count"]))
        else:
            update["model_response_attempt"] = 0
        return update

    @staticmethod
    def _completion(status: TaskRunStatus, reason_code: ReasonCode, summary: str,
                    cycle_count: int) -> dict[str, object]:
        return {"completion": TaskAgentCompletion(status=status,
                    result=TaskRunResult(reason_code=reason_code, summary=summary or reason_code.value),
                    cycle_count=cycle_count), "route": "end"}


def _message_has_image(message: BaseMessage) -> bool:
    return any(block.get("type") == "image" for block in message.content_blocks)


def _collect_expired_image_updates(
    messages: Sequence[BaseMessage], history_rounds: int,
) -> list[BaseMessage]:
    """Replace expired images by artifact references via same-ID State updates.

    Called after tool completion. The public message reducer owns replacement;
    this boundary only owns the product's image-round retention policy.
    """
    if history_rounds < 1:
        raise ValueError("Image history must retain at least one round")
    image_rounds: list[int] = []
    decision_round = 0
    message_rounds: list[int] = []
    for message in messages:
        if isinstance(message, AIMessage):
            decision_round += 1
        message_rounds.append(decision_round)
        if _message_has_image(message) and decision_round not in image_rounds:
            image_rounds.append(decision_round)
    retained_rounds = set(image_rounds[-history_rounds:])
    replacements: list[BaseMessage] = []
    for message, decision_round in zip(messages, message_rounds, strict=True):
        if decision_round in retained_rounds or not _message_has_image(message):
            continue
        if not message.id:
            raise ValueError("Image retention requires a message id")
        replacement = message.model_copy(deep=True)
        for block in replacement.content_blocks:
            if block.get("type") == "image" and (not isinstance(block.get("id"), str) or not block.get("id")):
                raise ValueError("Image retention requires an Artifact id")
        replacement.content = [
            {"type": "text", "text": "[Image was saved; image is outside the image history window.]",
             "extras": {"artifact_id": block.get("id")}}
            if block.get("type") == "image" else dict(block)
            for block in replacement.content_blocks
        ]
        replacements.append(replacement)
    return replacements


def _latest_ai_call(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and len(message.tool_calls) == 1:
            return dict(message.tool_calls[0])
    raise ValueError("No single AI tool call is available")

def _project_structured_tool_content(
    structured: JsonValue, image_references: dict[str, str],
) -> JsonValue:
    """Keep structured facts in model-visible content without nested image copies."""
    if isinstance(structured, str):
        return image_references.get(structured, structured)
    if isinstance(structured, list):
        return [_project_structured_tool_content(value, image_references) for value in structured]
    if isinstance(structured, dict):
        return {key: _project_structured_tool_content(value, image_references) for key, value in structured.items()}
    return structured
