from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.device import DeviceInfo
from app.domain.ids import TestCaseId, TestPlanId, TestTaskId
from app.domain.llm import LLMProfileSnapshot
from app.domain.test_cases import TestCaseContent


class TestTaskType(StrEnum):
    ACT = "act"
    JUDGE = "judge"


class TestPlanOrigin(StrEnum):
    PLANNING = "planning"
    REPLANNING = "replanning"
    MANUAL_REVISION = "manual_revision"


class TestTaskDefinition(BaseModel):
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
        if any(not item.strip() for item in self.success_criteria):
            raise ValueError("success_criteria must not contain blank values")
        return self


class TestTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_task_id: TestTaskId
    definition: TestTaskDefinition


TTask = TypeVar("TTask", TestTaskDefinition, TestTask)


class TestPlanContent(BaseModel, Generic[TTask]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    setup_steps: list[str]
    assumptions: list[str]
    tasks: list[TTask] = Field(min_length=1)

    @model_validator(mode="after")
    def content_is_valid(self) -> "TestPlanContent[TTask]":
        if not self.title.strip():
            raise ValueError("plan title must not be blank")
        if any(not item.strip() for item in self.setup_steps + self.assumptions):
            raise ValueError("setup_steps and assumptions must not contain blank values")
        if self.tasks and isinstance(self.tasks[0], TestTask):
            ids = [item.test_task_id for item in self.tasks if isinstance(item, TestTask)]
            if len(ids) != len(set(ids)):
                raise ValueError("test_task_id values must be unique")
        return self


class TestPlanPlanningContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_case_content: TestCaseContent
    device_info: DeviceInfo | None
    planning_model: LLMProfileSnapshot
    planning_prompt_version: str = Field(min_length=1)


class TestPlan(BaseModel):
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
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_case_content: TestCaseContent
    device_info: DeviceInfo | None


def identify_plan_content(
    content: TestPlanContent[TestTaskDefinition],
) -> TestPlanContent[TestTask]:
    return TestPlanContent[TestTask](
        title=content.title,
        setup_steps=content.setup_steps,
        assumptions=content.assumptions,
        tasks=[
            TestTask(test_task_id=str(uuid4()), definition=definition)
            for definition in content.tasks
        ],
    )
