from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, RetryPolicy
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from app.domain.errors import (
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
from app.domain.ids import ArtifactId
from app.domain.planning import TestTask, TestTaskType
from app.execution.signals import MessageValidationSignal, ToolStartedSignal
from app.artifacts import ArtifactStore
from app.llm import ChatModelClient, ModelProvider
from app.prompts import PromptDefinition


TaskRoute = Literal["guard", "model", "validate", "tools", "end"]
_FRAMEWORK_DEFAULT_RETRY = RetryPolicy().retry_on
_JSON_VALUE = TypeAdapter(JsonValue)


def _retry_model_error(exc: Exception) -> bool:
    """Extend the framework default for asyncio model-call timeouts."""

    if isinstance(exc, ModelContextBudgetExceeded):
        return False
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


def count_model_request_tokens(
    messages: Sequence[BaseMessage],
    *,
    tools: Sequence[BaseTool],
    model_profile: LLMProfileSnapshot,
) -> int:
    """用 LangChain 公共近似器计算消息 envelope、图片与完整工具 schema。"""

    return count_tokens_approximately(
        messages,
        chars_per_token=model_profile.characters_per_token,
        tokens_per_image=model_profile.tokens_per_image,
        tools=[*tools, finish_task],
    )


def select_model_message_window(
    messages: Sequence[BaseMessage],
    *,
    tools: Sequence[BaseTool],
    model_profile: LLMProfileSnapshot,
) -> list[BaseMessage]:
    """保留最新截图并在 profile 输入预算内裁剪原始 Message 对象。"""

    image_message_ids = [message.id for message in messages if _message_has_image(message)]
    selected = list(messages)
    input_budget = (
        model_profile.context_window_tokens
        - model_profile.max_output_tokens
        - model_profile.context_safety_margin_tokens
    )

    def token_counter(candidate_messages: list[BaseMessage]) -> int:
        return count_model_request_tokens(
            candidate_messages,
            tools=tools,
            model_profile=model_profile,
        )

    # The initial system prompt and task/success criteria are mandatory. Let the
    # framework trim complete conversational suffixes within their remaining budget.
    prefix_end = next(
        (index + 1 for index, message in enumerate(selected) if isinstance(message, HumanMessage)),
        0,
    )
    required = selected[:prefix_end]
    window = required + trim_messages(
        selected[prefix_end:],
        max_tokens=input_budget,
        token_counter=lambda candidates: token_counter([*required, *cast(list[BaseMessage], candidates)]),
        strategy="last",
        allow_partial=False,
        include_system=not required,
        start_on=("human", "ai"),
        end_on=("human", "tool"),
    )
    if token_counter(window) > input_budget:
        raise ModelContextBudgetExceeded(
            "The required system prompt, task context, latest screenshot and fixed tool schemas exceed the model input budget"
        )
    if any(isinstance(message, SystemMessage) for message in messages) and not any(
        isinstance(message, SystemMessage) for message in window
    ):
        raise ModelContextBudgetExceeded(
            "The system prompt and fixed tool schemas exceed the model input budget"
        )
    latest_image_id = image_message_ids[-1] if image_message_ids else None
    if latest_image_id is not None and all(
        message.id != latest_image_id for message in window
    ):
        raise ModelContextBudgetExceeded(
            "The latest screenshot and fixed tool schemas exceed the model input budget"
        )
    if messages and messages[-1] not in window:
        raise ModelContextBudgetExceeded("The latest tool result or task context exceeds the model input budget")
    # A trimmed suffix must keep every retained return paired with its call.
    retained_calls = {
        call["id"] for message in window if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    retained_returns = {message.tool_call_id for message in window if isinstance(message, ToolMessage)}
    if retained_calls != retained_returns:
        raise ModelContextBudgetExceeded("The tool call and return pair cannot fit the model input budget")
    return window


def _message_has_image(message: BaseMessage) -> bool:
    return any(block.get("type") == "image" for block in message.content_blocks)


def prune_screenshot_messages(
    messages: Sequence[BaseMessage], history_rounds: int,
) -> list[BaseMessage]:
    """Replace expired image blocks using add_messages IDs; preserve all other facts."""

    decision_indices = [i for i, message in enumerate(messages) if isinstance(message, AIMessage)]
    if len(decision_indices) <= history_rounds:
        return []
    cutoff = decision_indices[-history_rounds]
    replacements: list[BaseMessage] = []
    for message in messages[:cutoff]:
        if not _message_has_image(message):
            continue
        content: list[dict[str, Any]] = []
        for block in message.content_blocks:
            if block.get("type") == "image":
                content.append({
                    "type": "text", "text": "[Screenshot was captured; image is outside the visual history window.]",
                    "extras": {"artifact_id": block.get("id")},
                })
            else:
                content.append(dict(block))
        replacements.append(message.model_copy(update={"content": content}))
    return replacements


def project_structured_tool_content(
    structured: JsonValue, image_references: dict[str, str],
) -> JsonValue:
    """Keep structured facts in model-visible content without nested image copies."""
    if isinstance(structured, str):
        return image_references.get(structured, structured)
    if isinstance(structured, list):
        return [project_structured_tool_content(value, image_references) for value in structured]
    if isinstance(structured, dict):
        return {key: project_structured_tool_content(value, image_references) for key, value in structured.items()}
    return structured


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
        self, *, task_run_id: str, tools: tuple[BaseTool, ...],
        screenshot_history_rounds: int, task: TestTask, previous_task_runs: list[TaskRun],
        expected_model_profile: LLMProfileSnapshot, expected_prompt_version: str,
        run_cancellation_event: asyncio.Event, checkpointer: BaseCheckpointSaver[Any] | None,
    ) -> tuple[Any, TaskAgentGraphState]:
        try:
            model_client = self.model_provider.client_by_id(expected_model_profile.profile_id)
        except LookupError as exc:
            raise ModelProfileMismatch(str(exc)) from exc
        if model_client.profile_snapshot != expected_model_profile:
            raise ModelProfileMismatch("Resolved model does not match the TestRun snapshot")
        prompt = self.prompts_by_task_type[task.definition.type]
        if prompt.version != expected_prompt_version:
            raise PromptVersionMismatch(expected=expected_prompt_version, actual=prompt.version, prompt_name=prompt.name)
        runtime = _TaskRuntime(
            artifacts=self.artifacts, run_cancellation_event=run_cancellation_event,
            task_run_id=task_run_id, task=task, previous_task_runs=previous_task_runs,
            tools=tools, model_client=model_client, prompt_definition=prompt,
            model_response_max_attempts=self.model_response_max_attempts,
            tool_call_timeout_seconds=self.tool_call_timeout_seconds,
            screenshot_history_rounds=screenshot_history_rounds,
        )
        graph = StateGraph(TaskAgentGraphState)
        graph.add_node("initialize", runtime.initialize)
        graph.add_node("guard", runtime.guard)
        graph.add_node("model", runtime.call_model, retry_policy=RetryPolicy(
            initial_interval=0.25, backoff_factor=2, max_interval=1,
            max_attempts=self.model_call_max_attempts, jitter=False, retry_on=_retry_model_error,
        ))
        graph.add_node("validate", runtime.validate_message)
        graph.add_node("announce_tool", runtime.announce_tool)
        graph.add_node("tools", ToolNode([*tools, finish_task],
                                        awrap_tool_call=runtime.execute_tool))
        graph.add_node("after_tools", runtime.after_tools)
        graph.add_edge(START, "initialize")
        graph.add_edge("initialize", "guard")
        graph.add_conditional_edges("guard", lambda state: state["route"], {"model": "model", "end": END})
        graph.add_edge("model", "validate")
        graph.add_conditional_edges("validate", lambda state: state["route"],
                                    {"tools": "announce_tool", "guard": "guard", "end": END})
        graph.add_conditional_edges("announce_tool", lambda state: state["route"], {"tools": "tools", "end": END})
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges("after_tools", lambda state: state["route"], {"guard": "guard", "end": END})
        return graph.compile(checkpointer=checkpointer), {
            "messages": [], "cycle_count": 0, "model_attempt": 0, "completion": None, "route": "guard",
        }


@dataclass
class _TaskRuntime:
    artifacts: ArtifactStore
    run_cancellation_event: asyncio.Event
    task_run_id: str
    task: TestTask
    previous_task_runs: list[TaskRun]
    tools: tuple[BaseTool, ...]
    model_client: ChatModelClient
    prompt_definition: PromptDefinition
    model_response_max_attempts: int
    tool_call_timeout_seconds: float
    screenshot_history_rounds: int
    bound_model: Any = field(default=None, init=False)
    tool_interrupted: bool = field(default=False, init=False)

    async def initialize(self, state: TaskAgentGraphState) -> dict[str, object]:
        return {"messages": [
            SystemMessage(content=self.prompt_definition.text),
            HumanMessage(content=json.dumps({
                "task": self.task.model_dump(mode="json"),
                "previous_task_runs": [run.model_dump(mode="json") for run in self.previous_task_runs],
            }, ensure_ascii=False)),
        ], "route": "guard"}

    async def guard(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.run_cancellation_event.is_set():
            return self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state)
        if state["cycle_count"] >= self.task.definition.max_cycles:
            evidence = self._latest_artifact_ids(state["messages"])
            return self._completion(
                TaskRunStatus.FAILED if evidence else TaskRunStatus.BLOCKED,
                ReasonCode.CYCLE_LIMIT if evidence else ReasonCode.EVIDENCE_MISSING,
                "Decision round limit reached" if evidence else "Decision round limit reached without screenshot evidence",
                state, evidence=evidence,
            )
        return {"route": "model"}

    async def call_model(self, state: TaskAgentGraphState) -> dict[str, object]:
        if self.bound_model is None:
            self.bound_model = self.model_client.create_model().bind_tools([*self.tools, finish_task], parallel_tool_calls=False)
        try:
            response = await asyncio.wait_for(self.bound_model.ainvoke(select_model_message_window(
                state["messages"], tools=self.tools, model_profile=self.model_client.profile_snapshot,
            )), timeout=self.model_client.timeout_seconds)
        except TimeoutError as exc:
            raise ModelCallTimeout("Model call timed out") from exc
        if not isinstance(response, AIMessage):
            raise TypeError("chat model must return AIMessage")
        return {"messages": [response], "cycle_count": state["cycle_count"] + 1,
                "model_attempt": state["model_attempt"] + 1, "route": "validate"}

    async def validate_message(self, state: TaskAgentGraphState) -> dict[str, object]:
        message = state["messages"][-1]
        assert isinstance(message, AIMessage)
        replacements = prune_screenshot_messages(state["messages"], self.screenshot_history_rounds)
        reason: str | None = None
        if len(message.tool_calls) != 1 or message.invalid_tool_calls:
            reason = "response must contain exactly one valid tool call"
        else:
            call = message.tool_calls[0]
            if not call.get("id"):
                reason = "tool call requires a non-empty id"
            elif call["name"] not in {tool.name for tool in self.tools} | {finish_task.name}:
                reason = f"tool is not available: {call['name']}"
            elif call["name"] == finish_task.name:
                try:
                    terminal = FinishTaskRunToolInput.model_validate(call["args"])
                    if terminal.status in {"passed", "failed"} and not self._latest_artifact_ids(state["messages"]):
                        reason = "passed/failed requires an actual saved screenshot from this task"
                except ValueError as exc:
                    reason = str(exc)
        if reason is None:
            return {"messages": replacements, "route": "tools"}
        assert message.id is not None
        get_stream_writer()(MessageValidationSignal(message_id=message.id, attempt=state["model_attempt"], reason=reason).model_dump(mode="json"))
        if state["cycle_count"] >= self.task.definition.max_cycles:
            repairs = [ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"], status="error")
                       for call in message.tool_calls if call.get("id")]
            return {"messages": [*replacements, *repairs], "route": "guard"}
        if state["model_attempt"] >= self.model_response_max_attempts:
            repairs = [ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"], status="error")
                       for call in message.tool_calls if call.get("id")]
            return {"messages": [*replacements, *repairs],
                    **self._completion(TaskRunStatus.BLOCKED, ReasonCode.INVALID_MODEL_RESPONSE, reason, state)}
        repairs: list[BaseMessage] = [ToolMessage(content=reason, tool_call_id=call["id"], name=call["name"], status="error")
                                    for call in message.tool_calls if call.get("id")]
        repairs.append(HumanMessage(content=f"Return one allowed tool call with a new id. {reason}"))
        return {"messages": [*replacements, *repairs], "route": "guard"}

    async def announce_tool(self, state: TaskAgentGraphState) -> dict[str, object]:
        call = self._latest_ai_call(state["messages"])
        if self.run_cancellation_event.is_set():
            return {"messages": [ToolMessage(content="Cancelled before execution", tool_call_id=call["id"], name=call["name"], status="error")],
                    **self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled before tool execution", state)}
        get_stream_writer()(ToolStartedSignal(call_id=call["id"]).model_dump(mode="json"))
        return {"route": "tools"}

    async def execute_tool(
        self, request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Product boundary: bounded/cancellable tool execution and evidence ownership."""
        invocation = asyncio.ensure_future(execute(request))
        cancellation = asyncio.create_task(self.run_cancellation_event.wait())
        try:
            done, _ = await asyncio.wait({invocation, cancellation}, timeout=self.tool_call_timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if invocation not in done:
                self.tool_interrupted = True
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
                result = ToolMessage(
                    content="Tool execution cancelled or timed out; unfinished operation results are unconfirmed.",
                    tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error",
                )
            else:
                result = await invocation
            if not isinstance(result, ToolMessage):
                return result
            content: list[dict[str, Any]] = []
            image_references: dict[str, str] = {}
            for block in result.content_blocks:
                saved_block = dict(block)
                if block.get("type") == "image":
                    encoded = block.get("base64")
                    mime_type = block.get("mime_type")
                    if not isinstance(encoded, str) or not isinstance(mime_type, str):
                        raise ValueError("Screenshot evidence must contain inline base64 and MIME type")
                    raw = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
                    artifact = await self.artifacts.save_screenshot(task_run_id=self.task_run_id, content=raw, mime_type=mime_type)
                    saved_block["id"] = artifact.id
                    image_references[encoded] = f"[Image artifact {artifact.id}]"
                    image_references[f"data:{mime_type};base64,{encoded}"] = f"[Image artifact {artifact.id}]"
                content.append(saved_block)
            # The adapter carries structuredContent in artifact. Project distinct
            # facts to text once, and clear the opaque duplicate from checkpoints.
            if isinstance(result.artifact, dict) and "structured_content" in result.artifact:
                structured = _JSON_VALUE.validate_python(result.artifact["structured_content"])
                projected = project_structured_tool_content(structured, image_references)
                already_in_text = False
                for block in content:
                    if block.get("type") == "text":
                        try:
                            if json.loads(block["text"]) == structured:
                                already_in_text = True
                                block["text"] = json.dumps(projected, ensure_ascii=False)
                        except (ValueError, TypeError):
                            pass
                if not already_in_text:
                    content.append({"type": "text", "text": json.dumps(projected, ensure_ascii=False)})
            return result.model_copy(update={"content": content, "artifact": None})
        except Exception as exc:
            self.tool_interrupted = True
            return ToolMessage(content=f"Tool result unavailable: {exc}", tool_call_id=request.tool_call["id"],
                               name=request.tool_call["name"], status="error")
        finally:
            cancellation.cancel()
            if not invocation.done():
                invocation.cancel()
            await asyncio.gather(invocation, cancellation, return_exceptions=True)

    async def after_tools(self, state: TaskAgentGraphState) -> dict[str, object]:
        tool_message = state["messages"][-1]
        assert isinstance(tool_message, ToolMessage)
        changes: dict[str, object] = {"messages": prune_screenshot_messages(state["messages"], self.screenshot_history_rounds)}
        if self.run_cancellation_event.is_set():
            return {**changes, **self._completion(TaskRunStatus.CANCELLED, ReasonCode.USER_CANCELLED, "Cancelled by user", state)}
        if self.tool_interrupted:
            return {**changes, **self._completion(TaskRunStatus.BLOCKED, ReasonCode.TOOL_FAILED, tool_message.text, state)}
        call = self._latest_ai_call(state["messages"])
        if call["name"] == finish_task.name and tool_message.status != "error":
            terminal = FinishTaskRunToolInput.model_validate(call["args"])
            status = TaskRunStatus(terminal.status)
            reason = {TaskRunStatus.PASSED: ReasonCode.COMPLETED, TaskRunStatus.BLOCKED: ReasonCode.AGENT_BLOCKED,
                      TaskRunStatus.FAILED: ReasonCode.GOAL_UNREACHABLE if self.task.definition.type == TestTaskType.ACT else ReasonCode.ASSERTION_FAILED}[status]
            evidence = self._latest_artifact_ids(state["messages"]) if status != TaskRunStatus.BLOCKED else []
            return {**changes, **self._completion(status, reason, terminal.summary, state, evidence=evidence)}
        if tool_message.status == "error":
            if state["cycle_count"] >= self.task.definition.max_cycles:
                return {**changes, "route": "guard"}
            if state["model_attempt"] >= self.model_response_max_attempts:
                return {**changes, **self._completion(TaskRunStatus.BLOCKED, ReasonCode.TOOL_FAILED, tool_message.text, state)}
            return {**changes, "route": "guard"}
        return {**changes, "model_attempt": 0, "route": "guard"}

    @staticmethod
    def _latest_ai_call(messages: Sequence[BaseMessage]) -> dict[str, Any]:
        for message in reversed(messages):
            if isinstance(message, AIMessage) and len(message.tool_calls) == 1:
                return dict(message.tool_calls[0])
        raise ValueError("No single AI tool call is available")

    @staticmethod
    def _latest_artifact_ids(messages: Sequence[BaseMessage]) -> list[ArtifactId]:
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                continue
            ids: list[ArtifactId] = []
            for block in message.content_blocks:
                value = block.get("id") if block.get("type") == "image" else (block.get("extras") or {}).get("artifact_id")
                if isinstance(value, str):
                    ids.append(cast(ArtifactId, value))
            if ids:
                return ids
        return []

    @staticmethod
    def _completion(status: TaskRunStatus, reason_code: ReasonCode, summary: str,
                    state: TaskAgentGraphState, *, evidence: list[ArtifactId] | None = None) -> dict[str, object]:
        return {"completion": TaskAgentCompletion(status=status,
                    result=TaskRunResult(reason_code=reason_code, summary=summary or reason_code.value,
                                         evidence_artifact_ids=evidence or []),
                    cycle_count=state["cycle_count"]), "route": "end"}
