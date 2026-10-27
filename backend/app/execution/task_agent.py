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
    ToolCall,
    ToolMessage,
)
from langchain_core.messages.content import ImageContentBlock, create_image_block
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, MessagesState, StateGraph, add_messages
from langgraph.graph.message import Messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime
from langgraph.types import Command, RetryPolicy
# Documented public API; LangGraph 1.2.9 omits this re-export from its type declarations.
from langgraph.types import default_retry_on  # type: ignore[reportPrivateImportUsage]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from app.domain.errors import (
    ToolCallError,
    ModelContextBudgetExceeded,
    ModelCallTimeout,
)
from app.domain.execution import (
    TaskAgentCompletion,
    ReasonCode,
    TaskRunResult,
    TaskRunStatus,
)
from app.domain.resources.static_resources import ResourceCatalogSnapshot, ResourceDefinitionSnapshot
from app.skill import top_level_skills
from app.domain.ids import ArtifactId, ModelProfileId, TaskRunId
from app.domain.planning import TestTask, TestTaskType
from app.execution.model_context import ModelContextWindow, ModelImageWindow
from app.execution.task_agent_events import TaskAgentEventRecorder
from app.artifacts import ArtifactStore
from app.llm import ChatModelClient, ModelProvider
from app.prompts import PromptCatalog, PromptDefinition


_JSON_VALUE = TypeAdapter(JsonValue)
MESSAGE_PUBLISHING_NODES = frozenset({
    "initialize", "cycle_guard", "model", "validate", "announce_tool", "after_tools",
})


class TaskAgentGraphState(MessagesState):
    cycle_count: int
    model_response_attempt: int
    completion: TaskAgentCompletion | None
    route: Literal["cycle_guard", "model", "tools", "end"]


class FinishTaskRunToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    summary: str = Field(min_length=1)
    memory: str | None = None


@tool(args_schema=FinishTaskRunToolInput)
async def finish_task(status: str, summary: str, memory: str | None = None) -> str:
    """Finish this task with passed, failed or blocked status.

    For every status, summary must describe the observations and reasoning supporting
    the selected conclusion, and how they relate to this task's success criteria.
    Optionally retain observations needed by later tasks in memory, including actual
    values, their source and relevant image artifact IDs. Do not copy the full history.
    """

    receipt = f"{status}: {summary}"
    if memory and memory.strip():
        receipt += f"\n\nSubmitted memory:\n{memory}"
    return receipt


finish_task.metadata = {"tool_source": "builtin:task_agent"}


@dataclass(frozen=True)
class TaskAgentContext:
    """Runtime context projected by the executor from persisted task/environment facts.

    This task-local input is not persisted; the plan, run environment, and
    completed TaskRuns remain authoritative. Live dependencies stay separate.
    """

    task_run_id: TaskRunId
    task: TestTask
    memory_text: str
    max_cycles: int


@dataclass(frozen=True)
class TaskAgentRuntimeContext:
    """Per-task framework injection boundary, sharing task facts and live image access."""

    task_context: TaskAgentContext
    artifacts: ArtifactStore


@tool
async def read_image_artifact(
    artifact_id: ArtifactId, runtime: ToolRuntime[TaskAgentRuntimeContext, TaskAgentGraphState],
) -> list[ImageContentBlock]:
    """Read an original saved image by its artifact_id within this run.

    Copy the artifact ID disclosed before an image or retained in task memory.
    This returns a previously saved image, not a fresh observation. A reference
    image remains a reference; rereading it does not make it an observed target.
    """
    try:
        content, mime_type = await runtime.context.artifacts.load_image_for_task(
            artifact_id, task_run_id=runtime.context.task_context.task_run_id,
        )
    except LookupError as exc:
        raise ToolException(str(exc)) from exc
    return [create_image_block(id=artifact_id, base64=base64.b64encode(content).decode("ascii"), mime_type=mime_type)]


read_image_artifact.handle_tool_error = True
read_image_artifact.metadata = {"tool_source": "builtin:task_agent"}
TASK_AGENT_TOOLS = (read_image_artifact, finish_task)


@dataclass(frozen=True)
class TaskAgentInput:
    """Factory input with task facts, external capabilities and the run's model selection."""

    task_context: TaskAgentContext
    tools: tuple[BaseTool, ...]
    resource_catalog: ResourceCatalogSnapshot
    model_profile_id: ModelProfileId


class TaskAgentFactory:
    def __init__(
        self, *, artifacts: ArtifactStore, model_provider: ModelProvider,
        prompt_catalog: PromptCatalog,
        tool_call_timeout_seconds: float = 120,
        model_call_max_attempts: int = 3, model_response_max_attempts: int = 3,
        image_history_rounds: int = 3,
    ) -> None:
        self.artifacts = artifacts
        self.model_provider = model_provider
        self.prompt_catalog = prompt_catalog
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self.model_call_max_attempts = model_call_max_attempts
        self.model_response_max_attempts = model_response_max_attempts
        self.image_history_rounds = image_history_rounds

    async def build(
        self, *, task_input: TaskAgentInput,
        run_cancellation_event: asyncio.Event, checkpointer: BaseCheckpointSaver[Any] | None,
        event_recorder: TaskAgentEventRecorder,
    ) -> tuple[CompiledStateGraph[TaskAgentGraphState, TaskAgentRuntimeContext], TaskAgentGraphState, TaskAgentRuntimeContext]:
        model_client = self.model_provider.client_by_id(task_input.model_profile_id)
        prompt = self.prompt_catalog.prompt_for_task_type(task_input.task_context.task.definition.type)
        runtime_context = TaskAgentRuntimeContext(task_context=task_input.task_context, artifacts=self.artifacts)
        runtime = _TaskRuntime(
            runtime_context=runtime_context, run_cancellation_event=run_cancellation_event,
            model_client=model_client, prompt_definition=prompt, tools=task_input.tools,
            available_skills=top_level_skills(task_input.resource_catalog),
            model_context_window=ModelContextWindow(model_profile=model_client.profile_snapshot, tools=task_input.tools),
            model_image_window=ModelImageWindow(history_rounds=self.image_history_rounds),
            model_response_max_attempts=self.model_response_max_attempts,
            event_recorder=event_recorder,
        )
        graph = StateGraph(TaskAgentGraphState, context_schema=TaskAgentRuntimeContext)
        graph.add_node("initialize", runtime.initialize)
        graph.add_node("cycle_guard", runtime.cycle_guard)
        graph.add_node("model", runtime.call_model, retry_policy=RetryPolicy(
            initial_interval=0.25, backoff_factor=2, max_interval=1,
            max_attempts=self.model_call_max_attempts, jitter=False, retry_on=_retry_model_error,
        ))
        graph.add_node("validate", runtime.validate_message)
        graph.add_node("announce_tool", runtime.announce_tool)
        nodes_before_tools = set(graph.nodes)
        graph.add_node(
            "tools", ToolNode(task_input.tools), timeout=self.tool_call_timeout_seconds,
            # LangGraph 1.2.9 supports NodeError injection but omits it from StateNode's type.
            error_handler=runtime.handle_tool_error,  # type: ignore[reportArgumentType]
        )
        framework_tool_nodes = set(graph.nodes) - nodes_before_tools - {"tools"}
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
        publishing_nodes = set(graph.nodes) - framework_tool_nodes - {"tools", START, END}
        if publishing_nodes != MESSAGE_PUBLISHING_NODES:
            raise ValueError(
                "Message publishing nodes do not match graph: "
                f"missing={sorted(publishing_nodes - MESSAGE_PUBLISHING_NODES)}, "
                f"extra={sorted(MESSAGE_PUBLISHING_NODES - publishing_nodes)}"
            )
        return graph.compile(checkpointer=checkpointer), {
            "messages": [], "cycle_count": 0, "model_response_attempt": 0, "completion": None, "route": "cycle_guard",
        }, runtime_context


def _retry_model_error(exc: Exception) -> bool:
    """Extend the framework default for asyncio model-call timeouts."""

    if isinstance(exc, ModelContextBudgetExceeded):
        return False
    if isinstance(exc, TimeoutError):
        return True
    return default_retry_on(exc)


@dataclass
class _TaskRuntime:
    runtime_context: TaskAgentRuntimeContext
    run_cancellation_event: asyncio.Event
    model_client: ChatModelClient
    prompt_definition: PromptDefinition | None
    tools: tuple[BaseTool, ...]
    available_skills: list[ResourceDefinitionSnapshot]
    model_context_window: ModelContextWindow
    model_image_window: ModelImageWindow
    model_response_max_attempts: int
    event_recorder: TaskAgentEventRecorder
    bound_model: Any = field(default=None, init=False)

    async def initialize(self, state: TaskAgentGraphState) -> dict[str, object]:
        return {"messages": [
            SystemMessage(content=self.prompt_definition.text if self.prompt_definition is not None else ""),
            HumanMessage(content=json.dumps({
                "task": self.runtime_context.task_context.task.model_dump(mode="json"),
                "max_cycles": self.runtime_context.task_context.max_cycles,
                "memory_text": self.runtime_context.task_context.memory_text,
                "available_skills": [entry.model_dump(include={"source_id", "uri", "name", "description"})
                                     for entry in self.available_skills],
            }, ensure_ascii=False)),
        ], "route": "cycle_guard"}

    async def cycle_guard(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.run_cancellation_event.is_set():
            return self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state["cycle_count"])
        if state["cycle_count"] >= self.runtime_context.task_context.max_cycles:
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
        if state["cycle_count"] >= self.runtime_context.task_context.max_cycles:
            return {"messages": repairs, "route": "cycle_guard"}
        if state["model_response_attempt"] >= self.model_response_max_attempts:
            return {"messages": repairs,
                    **self._completion(TaskRunStatus.BLOCKED, ReasonCode.INVALID_MODEL_RESPONSE, reason, state["cycle_count"])}
        repairs.append(HumanMessage(content=f"Return one allowed tool call with a new id. {reason}"))
        return {"messages": repairs, "route": "cycle_guard"}

    async def announce_tool(self, state: TaskAgentGraphState) -> dict[str, object]:
        call = _latest_ai_call(state["messages"])
        if not self.run_cancellation_event.is_set():
            await self.event_recorder.record_tool_started(cast(str, call["id"]))
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

    async def _prepare_tool_result(
        self, result: ToolMessage, *, existing_artifact_id: ArtifactId | None = None,
    ) -> ToolMessage:
        """Save new images, disclose image identities and project content."""
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
                    artifact_id = existing_artifact_id
                    if artifact_id is None:
                        artifact = await self.runtime_context.artifacts.save_image(
                            task_run_id=self.runtime_context.task_context.task_run_id, content=raw, mime_type=mime_type,
                        )
                        artifact_id = artifact.id
                    saved_block["id"] = artifact_id
                    image_references[encoded] = f"[Image artifact {artifact_id}]"
                    image_references[f"data:{mime_type};base64,{encoded}"] = image_references[encoded]
                    content.append({"type": "text", "text": f"[Image artifact {artifact_id}]"})
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
        call = _latest_ai_call(state["messages"])
        existing_artifact_id = (call["args"]["artifact_id"]
                                if call["name"] == read_image_artifact.name and tool_message.status == "success" else None)
        tool_message = await self._prepare_tool_result(tool_message, existing_artifact_id=existing_artifact_id)
        messages = cast(list[BaseMessage], add_messages(cast(Messages, state["messages"]), [tool_message]))
        # Product image retention policy; add_messages replaces these messages
        # by ID without mutating earlier State snapshots or persisted events.
        replacements = self.model_image_window.collect_expired_updates(messages)
        update: dict[str, object] = {"route": "cycle_guard", "messages": [*replacements, tool_message]}
        if self.run_cancellation_event.is_set():
            update.update(self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state["cycle_count"]))
        elif call["name"] == finish_task.name and tool_message.status != "error":
            terminal = FinishTaskRunToolInput.model_validate(call["args"])
            status = TaskRunStatus(terminal.status)
            reason = {TaskRunStatus.PASSED: ReasonCode.COMPLETED, TaskRunStatus.BLOCKED: ReasonCode.AGENT_BLOCKED,
                      TaskRunStatus.FAILED: ReasonCode.GOAL_UNREACHABLE if self.runtime_context.task_context.task.definition.type == TestTaskType.ACT else ReasonCode.ASSERTION_FAILED}[status]
            update.update(self._completion(status, reason, terminal.summary, state["cycle_count"], memory=terminal.memory))
        elif tool_message.status == "error":
            if (state["cycle_count"] < self.runtime_context.task_context.max_cycles
                    and state["model_response_attempt"] >= self.model_response_max_attempts):
                update.update(self._completion(TaskRunStatus.BLOCKED, ReasonCode.TOOL_FAILED, tool_message.text, state["cycle_count"]))
        else:
            update["model_response_attempt"] = 0
        return update

    @staticmethod
    def _completion(status: TaskRunStatus, reason_code: ReasonCode, summary: str,
                    cycle_count: int, *, memory: str | None = None) -> dict[str, object]:
        return {"completion": TaskAgentCompletion(status=status,
                    result=TaskRunResult(reason_code=reason_code, summary=summary or reason_code.value, memory=memory),
                    cycle_count=cycle_count), "route": "end"}


def _latest_ai_call(messages: Sequence[BaseMessage]) -> ToolCall:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            if len(message.tool_calls) == 1:
                return message.tool_calls[0]
            break
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
