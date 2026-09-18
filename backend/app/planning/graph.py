"""Planning 过程的 LangGraph 实现；模型客户端由每次用例显式传入。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, TypedDict, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.exceptions import OutputParserException
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from app.domain.errors import PlanningFailure
from app.domain.planning import (
    TestCaseContent,
    TestPlanContent,
    TestTaskDefinition,
)
from app.llm import ChatModelClient
from app.prompts import PromptDefinition


PlanDraft = TestPlanContent[TestTaskDefinition]
_PLANNING_MAX_ATTEMPTS = 3


class PlanningAttemptProgress(TypedDict):
    """Graph custom-stream 边界；服务补充请求归属后验证为 PlanningEvent。"""

    type: Literal["planning.attempt_started", "planning.attempt_failed", "planning.validated"]
    attempt: int
    reason: NotRequired[str]


class PlanningGraphState(MessagesState):
    """单次规划 Graph 的框架状态，不持久化模型客户端或 prompt 对象。"""

    attempt: int
    result: PlanDraft | None
    error: str | None


class PlanningGraph:
    """用固定 PromptDefinition 与显式模型客户端生成 typed 计划草稿。"""

    def __init__(self, prompt_definition: PromptDefinition) -> None:
        self.prompt_definition = prompt_definition

    def _build(self, model_client: ChatModelClient) -> StateGraph:
        async def generate_node(
            state: PlanningGraphState,
        ) -> dict[str, object]:
            attempt = state.get("attempt", 0) + 1
            writer = get_stream_writer()
            writer(PlanningAttemptProgress(type="planning.attempt_started", attempt=attempt))
            model = model_client.create_model().with_structured_output(
                PlanDraft,
                method="json_mode",
            )
            try:
                value = await asyncio.wait_for(
                    model.ainvoke(state["messages"]),
                    timeout=model_client.timeout_seconds,
                )
                plan = (
                    value
                    if isinstance(value, TestPlanContent)
                    else PlanDraft.model_validate(value)
                )
                writer(PlanningAttemptProgress(type="planning.validated", attempt=attempt))
                return {"attempt": attempt, "result": plan, "error": None}
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    reason = "模型调用超时"
                elif isinstance(exc, (ValidationError, OutputParserException)):
                    reason = f"计划输出校验失败：{exc}"
                else:
                    reason = f"模型调用失败：{exc}"
                writer(PlanningAttemptProgress(type="planning.attempt_failed", attempt=attempt, reason=reason))
                return {
                    "attempt": attempt,
                    "error": reason,
                    "messages": [
                        HumanMessage(
                            content=(
                                "上次生成未成功。请严格按 TestPlanContent schema "
                                f"重新生成。原因：{reason}"
                            )
                        )
                    ],
                }

        graph = StateGraph(PlanningGraphState)
        graph.add_node("generate", generate_node)
        graph.add_edge(START, "generate")
        graph.add_conditional_edges(
            "generate",
            lambda state: "done"
            if state.get("result") is not None
            or state.get("attempt", 0) >= _PLANNING_MAX_ATTEMPTS
            else "retry",
            {"done": END, "retry": "generate"},
        )
        return graph

    async def generate(
        self,
        request: TestCaseContent,
        *,
        model_client: ChatModelClient,
        previous_plan: PlanDraft | None = None,
        user_input: str | None = None,
        on_progress: Callable[[PlanningAttemptProgress], Awaitable[None]] | None = None,
    ) -> PlanDraft:
        """以本 Graph 的固定 prompt 和本次唯一解析的客户端生成草稿。"""

        initial_messages: list[BaseMessage] = [
            SystemMessage(
                content=self.prompt_definition.text
                + "\n"
                + PydanticOutputParser(pydantic_object=PlanDraft).get_format_instructions()
            ),
            HumanMessage(content=request.model_dump_json()),
        ]
        if previous_plan is not None:
            initial_messages.append(AIMessage(content=previous_plan.model_dump_json()))
        initial_messages.append(HumanMessage(content=user_input or "无额外要求。"))
        with tracing_context(enabled=False):
            result: PlanDraft | None = None
            error: str | None = None
            async for part in self._build(model_client).compile().astream(
                {
                    "messages": initial_messages,
                    "attempt": 0,
                    "result": None,
                    "error": None,
                },
                stream_mode=["updates", "custom"],
                version="v2",
            ):
                if part["type"] == "custom":
                    if on_progress is not None:
                        await on_progress(cast(PlanningAttemptProgress, part["data"]))
                elif part["type"] == "updates" and "generate" in part["data"]:
                    result = part["data"]["generate"].get("result")
                    error = part["data"]["generate"].get("error")
        if result is None:
            raise PlanningFailure(
                f"规划在 {_PLANNING_MAX_ATTEMPTS} 次尝试后失败：{error}"
            )
        return (
            result
            if isinstance(result, TestPlanContent)
            else PlanDraft.model_validate(result)
        )
