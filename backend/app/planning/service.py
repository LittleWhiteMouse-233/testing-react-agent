"""规划用例输入投影、单次模型选择和不可变 TestPlan 创建编排。"""

from __future__ import annotations

from app.domain.activity import AgentActivity
from app.domain.planning import (
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTaskDefinition,
)
from app.llm import ModelProvider
from app.persistence.test_repository import SqlAlchemyTestRepository
from app.planning.graph import PlanningGraph


class PlanningService:
    """Planning 应用命令入口；输入、模型与 prompt 审计事实均只解析一次。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyTestRepository,
        planning_graph: PlanningGraph,
        model_provider: ModelProvider,
    ) -> None:
        self.repository = repository
        self.planning_graph = planning_graph
        self.model_provider = model_provider

    async def generate(self, *, test_case_id: str) -> TestPlan:
        test_case = await self.repository.get_test_case(test_case_id)
        model_client = self.model_provider.client_for_activity(
            AgentActivity.PLANNING
        )
        draft = await self.planning_graph.generate(
            test_case.content,
            model_client=model_client,
        )
        context = TestPlanPlanningContext(
            test_case_content=test_case.content,
            planning_model=model_client.profile_snapshot,
            planning_prompt_version=self.planning_graph.prompt_definition.version,
        )
        return await self.repository.create_plan(
            test_case_id=test_case_id,
            draft=draft,
            planning_context=context,
            origin=TestPlanOrigin.PLANNING,
        )

    async def revise(
        self,
        *,
        latest_test_plan_id: str,
        content: TestPlanContent[TestTaskDefinition],
    ) -> TestPlan:
        parent = await self.repository.get_test_plan(latest_test_plan_id)
        return await self.repository.create_plan(
            test_case_id=parent.test_case_id,
            draft=content,
            planning_context=parent.planning_context,
            origin=TestPlanOrigin.MANUAL_REVISION,
            derived_from_plan_id=parent.id,
        )
