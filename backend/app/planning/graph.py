"""Planning 过程的 LangGraph 实现；模型客户端由每次用例显式传入。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langgraph.graph import END, START, MessagesState, StateGraph
from langsmith import tracing_context

from app.domain.errors import PlanningFailure
from app.domain.planning import (
    TestCaseContent,
    TestPlanContent,
    TestTaskDefinition,
)
from app.llm import ChatModelClient
from app.prompts import PromptDefinition


PlanDraft = TestPlanContent[TestTaskDefinition]


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
                return {"attempt": attempt, "result": plan, "error": None}
            except Exception as exc:
                return {
                    "attempt": attempt,
                    "error": str(exc),
                    "messages": [
                        HumanMessage(
                            content=(
                                "上一输出未通过 TestPlanContent 校验。请严格按 schema "
                                f"重新生成。错误：{exc}"
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
            if state.get("result") is not None or state.get("attempt", 0) >= 3
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
            state = await self._build(model_client).compile().ainvoke(
                {
                    "messages": initial_messages,
                    "attempt": 0,
                    "result": None,
                    "error": None,
                }
            )
        result = state.get("result")
        if result is None:
            raise PlanningFailure(
                f"Planning failed after 3 attempts: {state.get('error')}"
            )
        return (
            result
            if isinstance(result, TestPlanContent)
            else PlanDraft.model_validate(result)
        )
