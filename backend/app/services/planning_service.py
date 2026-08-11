from __future__ import annotations

from app.device.contracts import DeviceProvider
from app.domain.activity import AgentActivity
from app.domain.planning import (
    PlanGenerationInput,
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTaskDefinition,
)
from app.graph.planning import PlanningGraph
from app.llm.registry import ModelRegistry
from app.persistence.execution_repository import SqlAlchemyExecutionRepository
from app.services.prompt_versions import prompt_version


class PlanningService:
    """Owns both planning input projection and TestPlan creation."""

    def __init__(
        self,
        *,
        repository: SqlAlchemyExecutionRepository,
        planning_graph: PlanningGraph,
        model_registry: ModelRegistry,
        devices: dict[str, DeviceProvider],
    ) -> None:
        self.repository = repository
        self.planning_graph = planning_graph
        self.model_registry = model_registry
        self.devices = devices

    async def generate(self, *, test_case_id: str, device_id: str) -> TestPlan:
        test_case = await self.repository.get_test_case(test_case_id)
        device = self.devices.get(device_id)
        if device is None:
            raise LookupError("Device not found")
        device_info = await device.describe()
        generation_input = PlanGenerationInput(
            test_case_content=test_case.content,
            device_info=device_info,
        )
        draft = await self.planning_graph.generate(generation_input)
        provider = self.model_registry.for_activity(AgentActivity.PLANNING)
        context = TestPlanPlanningContext(
            test_case_content=test_case.content,
            device_info=device_info,
            planning_model=provider.profile_snapshot,
            planning_prompt_version=prompt_version("planner"),
        )
        return await self.repository.create_plan(
            test_case_id=test_case_id,
            draft=draft,
            planning_context=context,
            # The repository finalizes planning/replanning beside version
            # allocation so concurrent model completions cannot mislabel origin.
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
