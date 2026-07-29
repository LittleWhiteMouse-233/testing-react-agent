from __future__ import annotations

from collections import deque
from typing import Any


from app.domain.models import (
    DECISION_ADAPTER,
    Decision,
    DecisionContext,
    PlanOutput,
    PlanRequest,
    Task,
    TaskPassDecision,
    TaskType,
)


class ScriptedLLMProvider:
    def __init__(
        self,
        plans: list[PlanOutput | dict[str, Any] | Exception] | None = None,
        decisions: list[Decision | dict[str, Any] | Exception] | None = None,
    ) -> None:
        self.plans = deque(plans or [])
        self.decisions = deque(decisions or [])
        self.model_info: dict[str, object] = {
            "provider": "scripted",
            "model": "deterministic",
        }

    async def plan(self, request: PlanRequest) -> PlanOutput:
        if self.plans:
            value = self.plans.popleft()
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, PlanOutput) else PlanOutput.model_validate(value)
        return PlanOutput(
            title=f"执行：{request.text[:80]}",
            setup_steps=["确认电视已开机并可被控制"],
            assumptions=[],
            tasks=[
                Task(
                    task_id="task-1",
                    type=TaskType.ACT,
                    title="到达目标页面",
                    goal=request.text,
                    success_criteria=["当前截图显示用例描述的目标页面或目标状态"],
                    max_cycles=10,
                ),
                Task(
                    task_id="task-2",
                    type=TaskType.JUDGE,
                    title="验证预期结果",
                    goal=request.text,
                    success_criteria=["当前截图满足用例中的预期结果"],
                    max_cycles=2,
                ),
            ],
        )

    async def decide(self, ctx: DecisionContext) -> Decision:
        if self.decisions:
            value = self.decisions.popleft()
            if isinstance(value, Exception):
                raise value
            return DECISION_ADAPTER.validate_python(value)
        return TaskPassDecision(
            type="task_pass",
            summary="脚本模型确认当前截图满足任务成功标准",
            evidence_artifact_ids=[ctx.artifact_id],
        )


