"""规划用例输入投影、单次模型选择和不可变 TestPlan 创建编排。"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.activity import AgentActivity
from app.domain.errors import PlanningUserInputRequired
from app.domain.planning import (
    PLANNING_EVENT_ADAPTER,
    PlanningEvent,
    PlanningFailedEvent,
    PlanningStageEvent,
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTaskDefinition,
    draft_plan_content,
)
from app import llm
from app.config import SettingsService
from app.event_stream import EventBus
from app.persistence.run_repository import SqlAlchemyRunRepository
from app.planning.graph import PlanningProgress, PlanningGraph
from app.domain.resources import CapabilityColumnSelection, select_capabilities
from app.resources.capabilities import CapabilityProvider
from app.test_case_lock import TestCaseLock


class PlanningService:
    """Planning 应用命令入口；输入、模型与 prompt 审计事实均只解析一次。"""

    def __init__(
        self,
        *,
        repository: SqlAlchemyRunRepository,
        test_case_lock: TestCaseLock,
        planning_graph: PlanningGraph,
        settings_service: SettingsService,
        capabilities: CapabilityProvider,
        planning_event_bus: EventBus[PlanningEvent],
    ) -> None:
        self.repository = repository
        self.test_case_lock = test_case_lock
        self.planning_graph = planning_graph
        self.settings_service = settings_service
        self.capabilities = capabilities
        self.planning_event_bus = planning_event_bus

    async def generate(
        self, *, test_case_id: str, planning_request_id: str,
        selected_columns: Sequence[CapabilityColumnSelection], user_input: str | None = None,
    ) -> TestPlan:
        async def publish(event: PlanningEvent) -> None:
            await self.planning_event_bus.publish(test_case_id, event)

        async def stage(event_type: str) -> None:
            await publish(PlanningStageEvent.model_validate({
                "test_case_id": test_case_id, "planning_request_id": planning_request_id,
                "type": event_type,
            }))

        async def graph_progress(progress: PlanningProgress) -> None:
            # Graph owns decision and tool facts; service owns request identity.
            await publish(PLANNING_EVENT_ADAPTER.validate_python({
                **progress, "test_case_id": test_case_id, "planning_request_id": planning_request_id,
            }))

        self.settings_service.ensure_available()
        with self.test_case_lock.hold(test_case_id, "planning", "pre_planning"):
            try:
                await stage("planning.started")
                test_case = await self.repository.get_test_case(test_case_id)
                previous_plan = await self.repository.get_latest_test_plan(test_case_id)
                user_input = (user_input or "").strip() or None
                if previous_plan is not None and user_input is None:
                    raise PlanningUserInputRequired("重新规划必须填写本轮额外输入")
                settings = self.settings_service.current
                planning_setting = settings.planning.model_copy(deep=True)
                model_profile = settings.llm.profile_for_activity(
                    AgentActivity.PLANNING,
                ).model_copy(deep=True)
                model_client = llm.create_model_client(model_profile)
                previous_content = draft_plan_content(previous_plan.content) if previous_plan else None
                prompt_definition = self.planning_graph.prompt_definition
                discovery = await self.capabilities.discover()
                capability_catalog = select_capabilities(discovery.catalog, selected_columns)
                resource_tools = tuple(available_tool for available_tool in discovery.tools
                                       if (available_tool.metadata or {}).get("tool_source") == "builtin:skill")
                context = TestPlanPlanningContext(
                    test_case_content=test_case.content,
                    planning_model=model_client.profile_snapshot,
                    planning_prompt_fingerprint=(
                        prompt_definition.fingerprint if prompt_definition is not None else None
                    ),
                    user_input=user_input,
                    planning_setting=planning_setting,
                    capability_catalog=capability_catalog,
                )
                self.test_case_lock.release(test_case_id, "pre_planning")
                await stage("planning.input_ready")
                draft = await self.planning_graph.generate(
                    context,
                    model_client=model_client,
                    previous_plan=previous_content,
                    resource_tools=resource_tools,
                    on_progress=graph_progress,
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
