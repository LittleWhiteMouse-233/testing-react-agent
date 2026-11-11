"""Bounded planning with reference-only execution capabilities and resource tools."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, TypedDict, cast

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_stream_writer
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, RetryPolicy
from langsmith import tracing_context

from app.domain.errors import ModelCallTimeout, PlanningFailure
from app.domain.planning import TestPlanContent, TestPlanPlanningContext, TestTaskDefinition
from app.domain.resources import top_level_skills
from app.llm import ChatModelClient, ModelContextWindow, should_retry_model_call
from app.resources.prompts import PromptDefinition


PlanDraft = TestPlanContent[TestTaskDefinition]


class PlanningProgress(TypedDict):
    """Graph custom-stream boundary; service adds request ownership and validates events."""

    type: Literal["planning.cycle_started", "planning.validated", "planning.response_invalid", "planning.tool_started", "planning.tool_completed"]
    cycle_count: int
    max_cycles: NotRequired[int]
    tool_call_id: NotRequired[str]
    tool_name: NotRequired[str]
    status: NotRequired[Literal["success", "error"]]
    reason: NotRequired[str | None]


class PlanningGraphState(MessagesState):
    cycle_count: int
    consecutive_errors: int
    result: PlanDraft | None
    error: str | None
    route: Literal["model", "tools", "cycle_guard", "end"]


@tool(args_schema=PlanDraft)
async def finish_plan(title: str, setup_steps: list[str], assumptions: list[str], tasks: list[TestTaskDefinition]) -> str:
    """Submit the complete semantic test plan, including all tasks and observable criteria.

    Describe task direction in natural language, never fixed key scripts or invented
    target observations. This submits a draft for validation and human confirmation.
    """
    return f"Plan accepted: {title}"


finish_plan.metadata = {"tool_source": "builtin:planning"}


class PlanningGraph:
    def __init__(self, prompt_definition: PromptDefinition | None) -> None:
        self.prompt_definition = prompt_definition

    def _build(
        self, model_client: ChatModelClient, planning_context: TestPlanPlanningContext,
        resource_tools: tuple[BaseTool, ...],
    ) -> StateGraph[PlanningGraphState, TestPlanPlanningContext]:
        settings = planning_context.planning_setting
        tools = (*resource_tools, finish_plan)
        bound_model = model_client.create_model().bind_tools(tools, parallel_tool_calls=False)
        context_window = ModelContextWindow(model_profile=model_client.profile_snapshot, tools=tools)
        tool_names = {available_tool.name for available_tool in tools}

        async def cycle_guard(state: PlanningGraphState) -> dict[str, object]:
            if state["cycle_count"] >= settings.max_cycles:
                return {"route": "end", "error": f"规划决策轮次耗尽（{settings.max_cycles} 轮）"}
            if state["consecutive_errors"] >= settings.model_response_max_attempts:
                return {"route": "end", "error": f"规划连续纠错额度耗尽：{state['error']}"}
            cycle_count = state["cycle_count"] + 1
            get_stream_writer()(PlanningProgress(type="planning.cycle_started", cycle_count=cycle_count, max_cycles=settings.max_cycles))
            return {"cycle_count": cycle_count, "route": "model"}

        async def call_model(state: PlanningGraphState) -> dict[str, object]:
            try:
                response = await asyncio.wait_for(
                    bound_model.ainvoke(context_window.select_messages(state["messages"])),
                    timeout=model_client.timeout_seconds,
                )
            except TimeoutError as exc:
                raise ModelCallTimeout("模型调用超时") from exc
            if not isinstance(response, AIMessage):
                raise TypeError("规划模型必须返回 AIMessage")
            return {"messages": [response]}

        async def validate_response(state: PlanningGraphState) -> dict[str, object]:
            response = state["messages"][-1]
            assert isinstance(response, AIMessage)
            reason: str | None = None
            if len(response.tool_calls) != 1 or response.invalid_tool_calls:
                reason = "response must contain exactly one valid tool call"
            else:
                call = response.tool_calls[0]
                if not call.get("id"):
                    reason = "tool call requires a non-empty id"
                elif call["name"] not in tool_names:
                    reason = f"tool is not available for planning: {call['name']}"
            if reason is None:
                return {"route": "tools"}
            get_stream_writer()(PlanningProgress(type="planning.response_invalid", cycle_count=state["cycle_count"], reason=reason))
            repairs: list[BaseMessage] = [
                ToolMessage(content=reason, tool_call_id=cast(str, call["id"]), name=call["name"], status="error")
                for call in response.tool_calls if call.get("id")
            ]
            if response.invalid_tool_calls or any(not call.get("id") for call in response.tool_calls):
                # An unparseable call cannot form the provider's call/return protocol.
                # Use the public reducer to replace it with a corrective business message.
                assert response.id is not None
                repairs = [RemoveMessage(id=response.id)]
            repairs.append(HumanMessage(content=f"Return one allowed tool call with a new id. {reason}"))
            return {"messages": repairs, "consecutive_errors": state["consecutive_errors"] + 1, "error": reason, "route": "cycle_guard"}

        async def announce_tool(state: PlanningGraphState) -> None:
            response = state["messages"][-1]
            assert isinstance(response, AIMessage)
            call = response.tool_calls[0]
            get_stream_writer()(PlanningProgress(type="planning.tool_started", cycle_count=state["cycle_count"], tool_call_id=cast(str, call["id"]), tool_name=call["name"]))

        def handle_tool_timeout(state: PlanningGraphState, error: NodeError) -> Command[Literal["after_tools"]]:
            if not isinstance(error.error, NodeTimeoutError):
                raise error.error
            response = state["messages"][-1]
            assert isinstance(response, AIMessage)
            call = response.tool_calls[0]
            return Command(update={"messages": [ToolMessage(
                content="Resource tool timed out; no result was received. No call was automatically replayed.",
                tool_call_id=cast(str, call["id"]), name=call["name"], status="error",
            )]}, goto="after_tools")

        async def after_tools(state: PlanningGraphState) -> dict[str, object]:
            result = state["messages"][-1]
            response = state["messages"][-2]
            assert isinstance(result, ToolMessage) and isinstance(response, AIMessage)
            call = response.tool_calls[0]
            get_stream_writer()(PlanningProgress(
                type="planning.tool_completed", cycle_count=state["cycle_count"],
                tool_call_id=cast(str, call["id"]), tool_name=call["name"], status=result.status,
                reason=result.text if result.status == "error" else None,
            ))
            if result.status == "error":
                return {"consecutive_errors": state["consecutive_errors"] + 1, "error": result.text, "route": "cycle_guard"}
            if call["name"] == finish_plan.name:
                draft = PlanDraft.model_validate(call["args"])
                get_stream_writer()(PlanningProgress(type="planning.validated", cycle_count=state["cycle_count"], max_cycles=settings.max_cycles))
                return {"result": draft, "error": None, "route": "end"}
            return {"consecutive_errors": 0, "error": None, "route": "cycle_guard"}

        graph = StateGraph(PlanningGraphState, context_schema=TestPlanPlanningContext)
        graph.add_node("cycle_guard", cycle_guard)
        graph.add_node("model", call_model, retry_policy=RetryPolicy(
            initial_interval=0.25, backoff_factor=2, max_interval=1,
            max_attempts=settings.model_call_max_attempts, jitter=False, retry_on=should_retry_model_call,
        ))
        graph.add_node("validate", validate_response)
        graph.add_node("announce_tool", announce_tool)
        graph.add_node("tools", ToolNode(tools), timeout=settings.tool_call_timeout_seconds,
                       error_handler=handle_tool_timeout)  # type: ignore[reportArgumentType]
        graph.add_node("after_tools", after_tools)
        graph.add_edge(START, "cycle_guard")
        graph.add_conditional_edges("cycle_guard", lambda state: state["route"], {"model": "model", "end": END})
        graph.add_edge("model", "validate")
        graph.add_conditional_edges("validate", lambda state: state["route"], {"tools": "announce_tool", "cycle_guard": "cycle_guard"})
        graph.add_edge("announce_tool", "tools")
        graph.add_edge("tools", "after_tools")
        graph.add_conditional_edges("after_tools", lambda state: state["route"], {"cycle_guard": "cycle_guard", "end": END})
        return graph

    async def generate(
        self, planning_context: TestPlanPlanningContext, *, model_client: ChatModelClient,
        resource_tools: tuple[BaseTool, ...], previous_plan: PlanDraft | None = None,
        on_progress: Callable[[PlanningProgress], Awaitable[None]] | None = None,
    ) -> PlanDraft:
        catalog = planning_context.capability_catalog
        initial_messages: list[AnyMessage] = [
            SystemMessage(content=self.prompt_definition.text if self.prompt_definition is not None else ""),
            HumanMessage(content=json.dumps({
                "test_case_content": planning_context.test_case_content.model_dump(mode="json"),
                "previous_plan": previous_plan.model_dump(mode="json") if previous_plan else None,
                "user_input": planning_context.user_input,
                "execution_tools": [definition.model_dump(mode="json") for definition in catalog.tool_catalog.tools
                                    if definition.source_id.startswith("mcp:") and catalog.column_is_selected(definition.source_id)],
                "available_skills": [skill.model_dump(include={"source_id", "uri", "name", "description"})
                                     for skill in top_level_skills(catalog.visible_resources())],
            }, ensure_ascii=False)),
        ]
        result: PlanDraft | None = None
        error: str | None = None
        initial_state = PlanningGraphState(
            messages=initial_messages, cycle_count=0, consecutive_errors=0,
            result=None, error=None, route="cycle_guard",
        )
        try:
            with tracing_context(enabled=False):
                async for part in self._build(model_client, planning_context, resource_tools).compile().astream(
                    initial_state,
                    context=planning_context,
                    config={"recursion_limit": planning_context.planning_setting.max_cycles * 8 + 20},
                    stream_mode=["updates", "custom"], version="v2",
                ):
                    if part["type"] == "custom" and on_progress is not None:
                        await on_progress(cast(PlanningProgress, part["data"]))
                    elif part["type"] == "updates":
                        for update in part["data"].values():
                            if not isinstance(update, dict):
                                continue
                            if update.get("result") is not None:
                                result = update["result"]
                            if "error" in update:
                                error = update["error"]
        except Exception as exc:
            raise PlanningFailure(f"规划调用失败：{exc}") from exc
        if result is None:
            raise PlanningFailure(error or "规划未提交完整计划")
        return PlanDraft.model_validate(result)
