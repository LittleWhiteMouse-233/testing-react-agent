"""Planning 过程的 LangGraph 实现；模型客户端由每次用例显式传入。"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage
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
                method="json_schema",
                strict=True,
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
    ) -> PlanDraft:
        """以本 Graph 的固定 prompt 和本次唯一解析的客户端生成草稿。"""

        initial_messages = [
            SystemMessage(content=self.prompt_definition.text),
            HumanMessage(
                content=json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
            ),
        ]
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
