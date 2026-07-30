from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, ToolRuntime

from app.device.contracts import DeviceController
from app.domain.errors import (
    ActionTimeout,
    ReasonCode,
    TransientToolError,
)
from app.domain.events import (
    AgentActionSelectedEvent,
    AgentResponseInvalidEvent,
    AgentTerminalSelectedEvent,
    CycleStartedEvent,
    ExecutionErrorEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from app.domain.execution import (
    ObservationRef,
    TaskOutcome,
    TaskStatus,
    TaskTerminalDecision,
)
from app.domain.planning import Task, TaskType
from app.domain.tools import (
    DeviceCapabilities,
    RemoteKey,
    ToolContext,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolInvocation,
)
from app.execution.ports import ArtifactRepository, ExecutionJournal, ExecutionRepository
from app.llm.contracts import ChatModelProvider
from app.services.registry import RunRegistry
from app.services.tools import ToolProvider


class TaskAgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    task: dict[str, Any]
    policy_id: str
    cycle_count: int
    model_attempt: int
    latest_observation: dict[str, Any] | None
    latest_tool_result: dict[str, Any] | None
    terminal_outcome: dict[str, Any] | None
    route: str


@dataclass(frozen=True)
class TaskAgentPolicy:
    policy_id: str
    prompt_name: str
    allow_device_keys: bool
    allow_input_text: bool
    allow_mutating_external_tools: bool


ACT_POLICY = TaskAgentPolicy(
    policy_id="act",
    prompt_name="act.txt",
    allow_device_keys=True,
    allow_input_text=True,
    allow_mutating_external_tools=True,
)
JUDGE_POLICY = TaskAgentPolicy(
    policy_id="judge",
    prompt_name="judge.txt",
    allow_device_keys=False,
    allow_input_text=False,
    allow_mutating_external_tools=False,
)


def policy_for(task: Task) -> TaskAgentPolicy:
    return ACT_POLICY if task.type == TaskType.ACT else JUDGE_POLICY


def _prompt(name: str) -> str:
    return (
        Path(__file__).resolve().parents[1] / "prompts" / name
    ).read_text("utf-8")


class TaskAgentFactory:
    def __init__(
        self,
        *,
        repository: ExecutionRepository,
        journal: ExecutionJournal,
        artifacts: ArtifactRepository,
        model_provider: ChatModelProvider,
        tool_provider: ToolProvider,
        registry: RunRegistry,
        devices: dict[str, DeviceController],
        action_timeout_seconds: float = 15,
        history_max_tokens: int = 8_000,
    ) -> None:
        self.repository = repository
        self.journal = journal
        self.artifacts = artifacts
        self.model_provider = model_provider
        self.tool_provider = tool_provider
        self.registry = registry
        self.devices = devices
        self.action_timeout_seconds = action_timeout_seconds
        self.history_max_tokens = history_max_tokens

    def build(
        self,
        *,
        run_id: str,
        task_run_id: str,
        device_id: str,
        task: Task,
        capabilities: DeviceCapabilities,
        enabled_tool_names: set[str],
        cross_task_context: list[dict[str, Any]],
        checkpointer: BaseCheckpointSaver | None,
    ) -> Any:
        policy = policy_for(task)
        device = self.devices.get(device_id)
        if device is None:
            raise LookupError(f"Device is not registered: {device_id}")
        runtime = _TaskRuntime(
            factory=self,
            run_id=run_id,
            task_run_id=task_run_id,
            device_id=device_id,
            task=task,
            policy=policy,
            capabilities=capabilities,
            enabled_tool_names=enabled_tool_names,
            cross_task_context=cross_task_context,
            device=device,
        )
        tools = runtime.build_tools()
        tool_node = ToolNode(
            tools,
            handle_tool_errors=(ValueError, TypeError),
        )
        bound_model = self.model_provider.create_model().bind_tools(
            tools,
            parallel_tool_calls=False,
            response_format=TaskTerminalDecision,
        )

        async def call_model_node(state: TaskAgentState) -> dict[str, Any]:
            return await runtime.call_model(state, bound_model)

        graph = StateGraph(TaskAgentState)
        graph.add_node("guard_and_observe", runtime.guard_and_observe)
        graph.add_node("call_model", call_model_node)
        graph.add_node("tools", tool_node)
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
            {"tools": "tools", "retry": "call_model", "end": END},
        )
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges(
            "after_tools",
            lambda state: state["route"],
            {
                "observe": "guard_and_observe",
                "retry": "call_model",
                "end": END,
            },
        )
        return graph.compile(checkpointer=checkpointer)


@dataclass
class _TaskRuntime:
    factory: TaskAgentFactory
    run_id: str
    task_run_id: str
    device_id: str
    task: Task
    policy: TaskAgentPolicy
    capabilities: DeviceCapabilities
    enabled_tool_names: set[str]
    cross_task_context: list[dict[str, Any]]
    device: DeviceController

    def build_tools(self) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        if self.policy.allow_device_keys:
            tools.append(
                self._structured_tool(
                    name="device_press_key",
                    description="Press one supported Android TV remote key.",
                    schema={
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "enum": [key.value for key in self.capabilities.supported_keys],
                            },
                            "decision_summary": {"type": "string", "minLength": 1},
                        },
                        "required": ["key", "decision_summary"],
                        "additionalProperties": False,
                    },
                    target_name="device_press_key",
                )
            )
        if self.policy.allow_input_text and self.capabilities.input_text:
            tools.append(
                self._structured_tool(
                    name="device_input_text",
                    description="Enter text into the currently focused TV input.",
                    schema={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "decision_summary": {"type": "string", "minLength": 1},
                        },
                        "required": ["text", "decision_summary"],
                        "additionalProperties": False,
                    },
                    target_name="device_input_text",
                )
            )
        tools.append(
            self._structured_tool(
                name="device_wait",
                description="Wait briefly without changing the tested business state.",
                schema={
                    "type": "object",
                    "properties": {
                        "duration_ms": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 10_000,
                        },
                        "decision_summary": {"type": "string", "minLength": 1},
                    },
                    "required": ["duration_ms", "decision_summary"],
                    "additionalProperties": False,
                },
                target_name="device_wait",
            )
        )
        reserved = {"device_press_key", "device_input_text", "device_wait"}
        for definition in self.factory.tool_provider.list_tools():
            if definition.name not in self.enabled_tool_names:
                continue
            if definition.name in reserved:
                raise ValueError(f"External tool uses reserved name: {definition.name}")
            if (
                not self.policy.allow_mutating_external_tools
                and definition.changes_device_state
            ):
                continue
            schema = _schema_with_summary(definition.input_schema)
            tools.append(
                self._structured_tool(
                    name=definition.name,
                    description=definition.description,
                    schema=schema,
                    target_name=definition.name,
                )
            )
        return tools

    def _structured_tool(
        self,
        *,
        name: str,
        description: str,
        schema: dict[str, Any],
        target_name: str,
    ) -> StructuredTool:
        async def execute(runtime: ToolRuntime, **kwargs: Any) -> str:
            summary = str(kwargs.pop("decision_summary"))
            result = await self.execute_tool(
                target_name,
                kwargs,
                summary,
                call_id=runtime.tool_call_id or f"{self.task_run_id}:{target_name}",
                cycle=int(runtime.state["cycle_count"]),
            )
            return result.model_dump_json()

        return StructuredTool.from_function(
            coroutine=execute,
            name=name,
            description=description,
            args_schema=schema,
            infer_schema=False,
        )

    async def guard_and_observe(self, state: TaskAgentState) -> dict[str, Any]:
        if self.factory.registry.cancellation(self.run_id).is_set():
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
        await self.factory.repository.update_cycle(
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
            await self.factory.journal.append(
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
        artifact = await self.factory.artifacts.save_observation(
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
            "messages": [
                HumanMessage(
                    content=(
                        f"Cycle {cycle_count}: captured observation "
                        f"artifact_id={artifact.id}, activity={screenshot.activity}."
                    )
                )
            ],
        }

    async def call_model(self, state: TaskAgentState, bound_model: Any) -> dict[str, Any]:
        attempt = state.get("model_attempt", 0) + 1
        observation = ObservationRef.model_validate(state["latest_observation"])
        raw, mime_type = await self.factory.artifacts.load_content(
            observation.artifact_id
        )
        encoded = base64.b64encode(raw).decode("ascii")
        history = _trim_history_preserving_tool_pairs(
            state.get("messages", []),
            self.factory.history_max_tokens,
        )
        context = {
            "task": self.task.model_dump(mode="json"),
            "policy": self.policy.policy_id,
            "cycle_count": state["cycle_count"],
            "max_cycles": self.task.max_cycles,
            "device_capabilities": self.capabilities.model_dump(mode="json"),
            "recent_cross_task_context": self.cross_task_context,
            "latest_artifact_id": observation.artifact_id,
        }
        messages = [
            SystemMessage(content=_prompt(self.policy.prompt_name)),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
            *history,
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
            ),
        ]
        try:
            response = await bound_model.ainvoke(messages)
        except Exception as exc:
            return await self._invalid_response(
                state, attempt, f"Model call failed: {exc}"
            )
        ai_message, terminal = _normalize_model_response(response)
        tool_calls = ai_message.tool_calls if ai_message is not None else []
        if tool_calls and terminal is not None:
            return await self._invalid_response(
                state, attempt, "Response contains both tool call and terminal output"
            )
        if len(tool_calls) > 1:
            return await self._invalid_response(
                state, attempt, "Only one tool call is allowed per cycle"
            )
        if len(tool_calls) == 1:
            call = tool_calls[0]
            available = {tool.name for tool in self.build_tools()}
            if call["name"] not in available:
                return await self._invalid_response(
                    state, attempt, f"Tool is not available: {call['name']}"
                )
            args = dict(call.get("args") or {})
            summary = str(args.pop("decision_summary", "")).strip()
            if not summary:
                return await self._invalid_response(
                    state, attempt, "Tool call requires decision_summary"
                )
            invocation = ToolInvocation(
                call_id=call["id"],
                name=call["name"],
                arguments=args,
                decision_summary=summary,
            )
            await self.factory.journal.append(
                AgentActionSelectedEvent(
                    run_id=self.run_id,
                    task_run_id=self.task_run_id,
                    cycle_count=state["cycle_count"],
                    invocation=invocation,
                ),
                dedup_key=f"{self.run_id}:{self.task_run_id}:{call['id']}:selected",
            )
            return {
                "route": "tools",
                "model_attempt": attempt,
                "messages": [ai_message],
            }
        if terminal is not None:
            evidence = (
                [observation.artifact_id]
                if terminal.status in {"passed", "failed"}
                else []
            )
            await self.factory.journal.append(
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
                    if self.policy.policy_id == "act"
                    else ReasonCode.ASSERTION_FAILED
                ),
                "blocked": ReasonCode.AGENT_BLOCKED,
            }[terminal.status]
            outcome = TaskOutcome(
                status=TaskStatus(terminal.status),
                reason_code=reason,
                summary=terminal.summary,
                cycle_count=state["cycle_count"],
                evidence_artifact_ids=evidence,
            )
            update = self._outcome_update(outcome)
            if ai_message is not None:
                update["messages"] = [ai_message]
            return update
        return await self._invalid_response(
            state, attempt, "Response contains neither a tool call nor terminal output"
        )

    async def after_tools(self, state: TaskAgentState) -> dict[str, Any]:
        last = state.get("messages", [])[-1]
        if not isinstance(last, ToolMessage):
            return await self._invalid_response(
                state,
                state.get("model_attempt", 0),
                "ToolNode did not produce ToolMessage",
            )
        try:
            result = ToolExecutionResult.model_validate_json(str(last.content))
        except Exception:
            return await self._invalid_response(
                state,
                state.get("model_attempt", 0),
                f"Tool arguments or result were invalid: {last.content}",
            )
        if result.status == ToolExecutionStatus.CANCELLED:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.SKIPPED,
                    reason_code=ReasonCode.USER_CANCELLED,
                    summary=result.summary,
                    cycle_count=state["cycle_count"],
                )
            )
        if result.status == ToolExecutionStatus.BLOCKED:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.TOOL_FAILED,
                    summary=result.summary,
                    cycle_count=state["cycle_count"],
                )
            )
        if result.status == ToolExecutionStatus.INVALID:
            return await self._invalid_response(
                state,
                state.get("model_attempt", 0),
                result.summary,
            )
        return {
            "route": "observe",
            "latest_tool_result": result.model_dump(mode="json"),
        }

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        decision_summary: str,
        *,
        call_id: str,
        cycle: int,
    ) -> ToolExecutionResult:
        invocation = ToolInvocation(
            call_id=call_id,
            name=name,
            arguments=arguments,
            decision_summary=decision_summary,
        )
        await self.factory.journal.append(
            ToolStartedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=cycle,
                invocation=invocation,
            ),
            dedup_key=self._dedup(cycle, f"tool.started:{name}"),
        )
        if self.factory.registry.cancellation(self.run_id).is_set():
            result = ToolExecutionResult(
                status=ToolExecutionStatus.CANCELLED,
                summary="Cancelled before tool execution",
            )
        else:
            try:
                raw_result = await asyncio.wait_for(
                    self._dispatch(name, arguments),
                    timeout=self.factory.action_timeout_seconds,
                )
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.SUCCEEDED,
                    summary=raw_result.summary,
                    data=raw_result.data,
                )
            except (TimeoutError, ActionTimeout) as exc:
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.TIMED_OUT,
                    summary=str(exc) or f"{name} timed out",
                )
            except (ValueError, TypeError) as exc:
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.INVALID,
                    summary=str(exc),
                )
            except Exception as exc:
                result = ToolExecutionResult(
                    status=ToolExecutionStatus.BLOCKED,
                    summary=str(exc),
                )
        await self.factory.journal.append(
            ToolFinishedEvent(
                run_id=self.run_id,
                task_run_id=self.task_run_id,
                cycle_count=cycle,
                invocation=invocation,
                result=result,
            ),
            dedup_key=self._dedup(cycle, f"tool.finished:{name}"),
        )
        return result

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "device_press_key":
            return await self.device.press(RemoteKey(arguments["key"]))
        if name == "device_input_text":
            return await self.device.input_text(str(arguments["text"]))
        if name == "device_wait":
            return await self.device.wait(int(arguments["duration_ms"]))
        last_error: Exception | None = None
        for _ in range(3):
            try:
                return await self.factory.tool_provider.execute(
                    name,
                    arguments,
                    ToolContext(
                        run_id=self.run_id,
                        task_run_id=self.task_run_id,
                        device_id=self.device_id,
                    ),
                )
            except TransientToolError as exc:
                last_error = exc
        raise last_error or RuntimeError(f"Tool execution failed: {name}")

    async def _invalid_response(
        self, state: TaskAgentState, attempt: int, message: str
    ) -> dict[str, Any]:
        await self.factory.journal.append(
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
        if attempt >= 3:
            return self._outcome_update(
                TaskOutcome(
                    status=TaskStatus.BLOCKED,
                    reason_code=ReasonCode.INVALID_MODEL_RESPONSE,
                    summary=f"Invalid model response after 3 attempts: {message}",
                    cycle_count=state["cycle_count"],
                )
            )
        return {
            "route": "retry",
            "model_attempt": attempt,
            "messages": [
                HumanMessage(
                    content=(
                        "The previous response was invalid. Return exactly one "
                        f"allowed tool call or the terminal schema. Error: {message}"
                    )
                )
            ],
        }

    @staticmethod
    def _outcome_update(outcome: TaskOutcome) -> dict[str, Any]:
        return {
            "route": "end",
            "terminal_outcome": outcome.model_dump(mode="json"),
        }

    def _dedup(self, cycle: int, suffix: str) -> str:
        return f"{self.run_id}:{self.task_run_id}:{cycle}:{suffix}"


def _schema_with_summary(schema: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(schema))
    result.setdefault("type", "object")
    properties = result.setdefault("properties", {})
    if "decision_summary" in properties:
        raise ValueError("Tool schema uses reserved field decision_summary")
    properties["decision_summary"] = {"type": "string", "minLength": 1}
    required = result.setdefault("required", [])
    if "decision_summary" not in required:
        required.append("decision_summary")
    result["additionalProperties"] = False
    return result


def _trim_history_preserving_tool_pairs(
    messages: list[AnyMessage],
    max_tokens: int,
) -> list[AnyMessage]:
    """Keep recent messages without splitting an AI tool call from its result."""
    units: list[list[AnyMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            call_ids = {call["id"] for call in message.tool_calls}
            unit = [message]
            cursor = index + 1
            while (
                cursor < len(messages)
                and isinstance(messages[cursor], ToolMessage)
                and messages[cursor].tool_call_id in call_ids
            ):
                unit.append(messages[cursor])
                cursor += 1
            if len(unit) > 1:
                units.append(unit)
            index = cursor
            continue
        if isinstance(message, ToolMessage):
            index += 1
            continue
        units.append([message])
        index += 1

    selected: list[list[AnyMessage]] = []
    token_count = 0
    for unit in reversed(units):
        unit_tokens = count_tokens_approximately(unit)
        if selected and token_count + unit_tokens > max_tokens:
            break
        selected.append(unit)
        token_count += unit_tokens
    return [message for unit in reversed(selected) for message in unit]


def _normalize_model_response(
    response: Any,
) -> tuple[AIMessage | None, TaskTerminalDecision | None]:
    if isinstance(response, dict) and "raw" in response:
        raw = response.get("raw")
        parsed = response.get("parsed")
        ai_message = raw if isinstance(raw, AIMessage) else None
        terminal = (
            parsed
            if isinstance(parsed, TaskTerminalDecision)
            else TaskTerminalDecision.model_validate(parsed)
            if parsed is not None
            else None
        )
        return ai_message, terminal
    if isinstance(response, TaskTerminalDecision):
        return None, response
    if not isinstance(response, AIMessage):
        try:
            return None, TaskTerminalDecision.model_validate(response)
        except Exception:
            return None, None
    parsed = response.additional_kwargs.get("parsed")
    terminal = (
        parsed
        if isinstance(parsed, TaskTerminalDecision)
        else TaskTerminalDecision.model_validate(parsed)
        if parsed is not None
        else None
    )
    return response, terminal
