"""规划用例输入投影、单次模型选择和不可变 TestPlan 创建编排。"""

from __future__ import annotations

from app.domain.activity import AgentActivity
from app.domain.errors import PlanningUserInputRequired
from app.domain.planning import (
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTaskDefinition,
    draft_plan_content,
)
from app.llm import ModelProvider
from app.domain.planning.events import PLANNING_EVENT_ADAPTER, PlanningEvent, PlanningFailedEvent, PlanningStageEvent
from app.event_stream import EventBus
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.planning.graph import PlanningAttemptProgress, PlanningGraph
from app.test_case_lock import TestCaseLock


class PlanningService:
    """Planning 应用命令入口；输入、模型与 prompt 审计事实均只解析一次。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyRunRepository,
        test_case_lock: TestCaseLock,
        planning_graph: PlanningGraph,
        model_provider: ModelProvider,
        planning_event_bus: EventBus[PlanningEvent],
    ) -> None:
        self.repository = repository
        self.test_case_lock = test_case_lock
        self.planning_graph = planning_graph
        self.model_provider = model_provider
        self.planning_event_bus = planning_event_bus

    async def generate(
        self, *, test_case_id: str, planning_request_id: str, user_input: str | None = None,
    ) -> TestPlan:
        async def publish(event: PlanningEvent) -> None:
            await self.planning_event_bus.publish(test_case_id, event)

        async def stage(event_type: str) -> None:
            await publish(PlanningStageEvent.model_validate({
                "test_case_id": test_case_id, "planning_request_id": planning_request_id,
                "type": event_type,
            }))

        async def attempt_progress(progress: PlanningAttemptProgress) -> None:
            # Semantic boundary: Graph owns attempt facts, service owns request identity.
            await publish(PLANNING_EVENT_ADAPTER.validate_python({
                **progress, "test_case_id": test_case_id, "planning_request_id": planning_request_id,
            }))

        with self.test_case_lock.hold(test_case_id, "planning"):
            try:
                await stage("planning.started")
                test_case = await self.repository.get_test_case(test_case_id)
                previous_plan = await self.repository.get_latest_test_plan(test_case_id)
                user_input = (user_input or "").strip() or None
                if previous_plan is not None and user_input is None:
                    raise PlanningUserInputRequired("重新规划必须填写本轮额外输入")
                model_client = self.model_provider.client_for_activity(
                    AgentActivity.PLANNING
                )
                await stage("planning.input_ready")
                draft = await self.planning_graph.generate(
                    test_case.content,
                    model_client=model_client,
                    previous_plan=draft_plan_content(previous_plan.content) if previous_plan else None,
                    user_input=user_input,
                    on_progress=attempt_progress,
                )
                context = TestPlanPlanningContext(
                    test_case_content=test_case.content,
                    planning_model=model_client.profile_snapshot,
                    planning_prompt_version=self.planning_graph.prompt_definition.version,
                    user_input=user_input,
                )
                await stage("planning.saving")
                plan = await self.repository.create_plan(
                    test_case_id=test_case_id,
                    draft=draft,
                    planning_context=context,
                    origin=TestPlanOrigin.REPLANNING if previous_plan else TestPlanOrigin.PLANNING,
                    derived_from_plan_id=previous_plan.id if previous_plan else None,
                )
                await stage("planning.succeeded")
                return plan
            except Exception as exc:
                await publish(PlanningFailedEvent(
                    test_case_id=test_case_id, planning_request_id=planning_request_id,
                    reason=str(exc) or "规划失败",
                ))
                raise

    async def revise(
        self,
        *,
        latest_test_plan_id: str,
        content: TestPlanContent[TestTaskDefinition],
    ) -> TestPlan:
        parent = await self.repository.get_test_plan(latest_test_plan_id)
        with self.test_case_lock.hold(parent.test_case_id, "revising"):
            return await self.repository.create_plan(
                test_case_id=parent.test_case_id,
                draft=content,
                planning_context=parent.planning_context,
                origin=TestPlanOrigin.MANUAL_REVISION,
                derived_from_plan_id=parent.id,
            )
