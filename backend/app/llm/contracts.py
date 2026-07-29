from __future__ import annotations

from backend.app.domain.models import DecisionContext, PlanOutput, PlanRequest


from typing import Protocol


from app.domain.models import (
    Decision,
)


class LLMProvider(Protocol):
    model_info: dict[str, object]

    async def plan(self, request: PlanRequest) -> PlanOutput: ...
    async def decide(self, ctx: DecisionContext) -> Decision: ...
