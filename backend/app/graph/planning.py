from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langsmith import tracing_context

from app.domain.activity import Activity
from app.domain.errors import PlanningFailure
from app.domain.planning import PlanOutput, PlanRequest
from app.llm.registry import ModelRegistry


class PlanningState(MessagesState):
    request: dict[str, Any]
    attempt: int
    result: dict[str, Any] | None
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
        graph = StateGraph(PlanningState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("generate", self._generate)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "generate")
        graph.add_conditional_edges(
            "generate",
            lambda state: "done"
            if state.get("result") is not None or state.get("attempt", 0) >= 3
            else "retry",
            {"done": END, "retry": "generate"},
        )
        return graph

    async def _prepare(self, state: PlanningState) -> dict[str, Any]:
        return {
            "messages": [
                HumanMessage(
                    content=json.dumps(state["request"], ensure_ascii=False)
                )
            ],
            "attempt": 0,
            "result": None,
            "error": None,
        }

    async def _generate(self, state: PlanningState) -> dict[str, Any]:
        attempt = state.get("attempt", 0) + 1
        provider = self.model_registry.for_activity(Activity.PLANNING)
        model = provider.create_model().with_structured_output(
            PlanOutput,
            method="json_schema",
            strict=True,
        )
        messages = [SystemMessage(content=self.prompt_text), *state["messages"]]
        try:
            value = await asyncio.wait_for(
                model.ainvoke(messages), timeout=provider.timeout_seconds
            )
            plan = value if isinstance(value, PlanOutput) else PlanOutput.model_validate(value)
            return {
                "attempt": attempt,
                "result": plan.model_dump(mode="json"),
                "error": None,
            }
        except Exception as exc:
            return {
                "attempt": attempt,
                "error": str(exc),
                "messages": [
                    HumanMessage(
                        content=(
                            "上一输出未通过 PlanOutput 校验。请严格按 Schema "
                            f"重新生成。错误：{exc}"
                        )
                    )
                ],
            }

    async def generate(self, request: PlanRequest) -> PlanOutput:
        with tracing_context(enabled=False):
            state = await self._graph.ainvoke(
                {
                    "messages": [],
                    "request": request.model_dump(mode="json"),
                    "attempt": 0,
                    "result": None,
                    "error": None,
                }
            )
        if state.get("result") is None:
            raise PlanningFailure(
                f"Planning failed after 3 attempts: {state.get('error')}"
            )
        return PlanOutput.model_validate(state["result"])
