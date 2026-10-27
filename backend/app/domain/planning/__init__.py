"""测试设计与规划进度事件的领域合同。

用例、不可变计划、任务及其转换由 ``test_design`` 拥有；
临时规划进度事件由 ``events`` 拥有。本包集中披露对外使用的领域合同。
"""

from app.domain.planning.events import (
    PLANNING_EVENT_ADAPTER,
    PlanningAttemptEvent,
    PlanningAttemptFailedEvent,
    PlanningEvent,
    PlanningEventBase,
    PlanningFailedEvent,
    PlanningStageEvent,
)
from app.domain.planning.test_design import (
    TestCase,
    TestCaseContent,
    TestPlan,
    TestPlanContent,
    TestPlanOrigin,
    TestPlanPlanningContext,
    TestTask,
    TestTaskDefinition,
    TestTaskType,
    draft_plan_content,
    identify_plan_content,
)

__all__ = [
    "PLANNING_EVENT_ADAPTER",
    "PlanningAttemptEvent",
    "PlanningAttemptFailedEvent",
    "PlanningEvent",
    "PlanningEventBase",
    "PlanningFailedEvent",
    "PlanningStageEvent",
    "TestCase",
    "TestCaseContent",
    "TestPlan",
    "TestPlanContent",
    "TestPlanOrigin",
    "TestPlanPlanningContext",
    "TestTask",
    "TestTaskDefinition",
    "TestTaskType",
    "draft_plan_content",
    "identify_plan_content",
]
