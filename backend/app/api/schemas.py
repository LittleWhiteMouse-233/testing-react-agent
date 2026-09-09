from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ids import TestPlanId
from app.domain.planning import TestCaseContent, TestPlanContent, TestTaskDefinition


T = TypeVar("T")


class TestCaseCreateRequest(TestCaseContent):
    """Command name over the canonical TestCase content fields."""


class ReviseTestPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: TestPlanContent[TestTaskDefinition]


class TestRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_plan_id: TestPlanId
    assumptions_confirmed: Literal[True]


class ReportExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["json", "html"]


class PageResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    total: int = Field(ge=0)


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
