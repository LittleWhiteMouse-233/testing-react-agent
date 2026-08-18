"""测试设计信息家族，拥有从 TestCase 到不可变 TestPlan 的领域合同。

本包只描述规划过程产生和冻结的业务事实；模型调用与持久化编排分别由
``app.planning`` 应用过程和 persistence 边界负责。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.activity import AgentActivity
from app.domain.ids import TestCaseId, TestPlanId, TestTaskId
from app.domain.resources.device import DeviceInfo
from app.domain.resources.llm import LLMProfileSnapshot


class TestTaskType(StrEnum):
    """计划任务的业务类型；执行过程据此选择 Agent activity。"""

    ACT = "act"
    JUDGE = "judge"


class TestPlanOrigin(StrEnum):
    """计划版本的产生方式，用于区分模型规划与人工修订历史。"""

    PLANNING = "planning"
    REPLANNING = "replanning"
    MANUAL_REVISION = "manual_revision"


class TestCaseContent(BaseModel):
    """用户提交的测试意图；由 TestCase 和规划上下文共同复用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def text_is_not_blank(self) -> "TestCaseContent":
        if not self.name.strip() or not self.source_text.strip():
            raise ValueError("test case name and source_text must not be blank")
        return self


class TestCase(BaseModel):
    """测试意图的持久化实体；后续 TestPlan 版本通过 ID 归属于它。"""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TestCaseId
    content: TestCaseContent
    created_at: datetime


class TestTaskDefinition(BaseModel):
    """没有身份的语义任务定义；模型或人工编辑先产生该结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: TestTaskType
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    max_cycles: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def text_is_not_blank(self) -> "TestTaskDefinition":
        if not self.title.strip() or not self.goal.strip():
            raise ValueError("task title and goal must not be blank")
        if any(not criterion.strip() for criterion in self.success_criteria):
            raise ValueError("success_criteria must not contain blank values")
        return self


class TestTask(BaseModel):
    """已获得不可变身份的计划任务；只存在于持久化 TestPlan 版本内。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_task_id: TestTaskId
    definition: TestTaskDefinition


TTask = TypeVar("TTask", TestTaskDefinition, TestTask)


class TestPlanContent(BaseModel, Generic[TTask]):
    """有序测试设计内容；泛型参数区分草稿任务与已定身份任务。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    setup_steps: list[str]
    assumptions: list[str]
    tasks: list[TTask] = Field(min_length=1)

    @model_validator(mode="after")
    def content_is_valid(self) -> "TestPlanContent[TTask]":
        if not self.title.strip():
            raise ValueError("plan title must not be blank")
        if any(not value.strip() for value in self.setup_steps + self.assumptions):
            raise ValueError(
                "setup_steps and assumptions must not contain blank values"
            )
        identified_tasks = [task for task in self.tasks if isinstance(task, TestTask)]
        task_ids = [task.test_task_id for task in identified_tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("test_task_id values must be unique")
        return self


class TestPlanPlanningContext(BaseModel):
    """计划创建时冻结的输入与模型审计事实，不参与后续版本覆盖。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_case_content: TestCaseContent
    device_info: DeviceInfo | None
    planning_model: LLMProfileSnapshot
    planning_prompt_version: str = Field(min_length=1)


class TestPlan(BaseModel):
    """一次不可变、可追溯的测试计划版本；执行仅允许使用最新版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: TestPlanId
    test_case_id: TestCaseId
    version_number: int = Field(ge=1)
    origin: TestPlanOrigin
    derived_from_plan_id: TestPlanId | None
    planning_context: TestPlanPlanningContext
    content: TestPlanContent[TestTask]
    created_at: datetime

    @model_validator(mode="after")
    def lineage_is_consistent(self) -> "TestPlan":
        if self.origin == TestPlanOrigin.MANUAL_REVISION:
            if self.derived_from_plan_id is None:
                raise ValueError("manual revision requires derived_from_plan_id")
        elif self.derived_from_plan_id is not None:
            raise ValueError("only manual revision may have derived_from_plan_id")
        return self


class PlanGenerationInput(BaseModel):
    """PlanningGraph 的领域输入边界，由 PlanningService 从当前资源投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_case_content: TestCaseContent
    device_info: DeviceInfo | None


def identify_plan_content(
    content: TestPlanContent[TestTaskDefinition],
) -> TestPlanContent[TestTask]:
    """为一个新计划版本的全部任务一次性分配全新身份。"""

    return TestPlanContent[TestTask](
        title=content.title,
        setup_steps=content.setup_steps,
        assumptions=content.assumptions,
        tasks=[
            TestTask(test_task_id=str(uuid4()), definition=definition)
            for definition in content.tasks
        ],
    )


def activity_for_task(task_type: TestTaskType) -> AgentActivity:
    """把规划过程的任务类型显式转换为 shared-kernel Agent activity。"""

    return AgentActivity(task_type.value)
