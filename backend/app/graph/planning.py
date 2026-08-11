from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langsmith import tracing_context

from app.domain.activity import AgentActivity
from app.domain.errors import PlanningFailure
from app.domain.planning import (
    PlanGenerationInput,
    TestPlanContent,
    TestTaskDefinition,
)
from app.llm.registry import ModelRegistry


PlanDraft = TestPlanContent[TestTaskDefinition]


class PlanningGraphState(MessagesState):
    attempt: int
    result: PlanDraft | None
    error: str | None


def _prompt() -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / "planner.txt"
    return path.read_text("utf-8")


class PlanningGraph:
    def __init__(self, model_registry: ModelRegistry) -> None:
        self.model_registry = model_registry
        self.prompt_text = _prompt()
        self._graph = self._build().compile()

    def _build(self) -> StateGraph:
        graph = StateGraph(PlanningGraphState)
        graph.add_node("generate", self._generate)
        graph.add_edge(START, "generate")
        graph.add_conditional_edges(
            "generate",
            lambda state: "done"
            if state.get("result") is not None or state.get("attempt", 0) >= 3
            else "retry",
            {"done": END, "retry": "generate"},
        )
        return graph

    async def _generate(self, state: PlanningGraphState) -> dict[str, object]:
        attempt = state.get("attempt", 0) + 1
        provider = self.model_registry.for_activity(AgentActivity.PLANNING)
        model = provider.create_model().with_structured_output(
            PlanDraft,
            method="json_schema",
            strict=True,
        )
        try:
            value = await asyncio.wait_for(
                model.ainvoke(state["messages"]), timeout=provider.timeout_seconds
            )
            plan = value if isinstance(value, TestPlanContent) else PlanDraft.model_validate(value)
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

    async def generate(self, request: PlanGenerationInput) -> PlanDraft:
        initial_messages = [
            SystemMessage(content=self.prompt_text),
            HumanMessage(
                content=json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
            ),
        ]
        with tracing_context(enabled=False):
            state = await self._graph.ainvoke(
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
        return result if isinstance(result, TestPlanContent) else PlanDraft.model_validate(result)
