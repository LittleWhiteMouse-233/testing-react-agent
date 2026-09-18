from __future__ import annotations

from datetime import date
from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import PlanningRequestId, TestCaseId, TestPlanId
from app.domain.planning import TestCase, TestCaseContent, TestPlanContent, TestTaskDefinition
from app.domain.execution import TaskRunStatus, TestRun, TestRunStatus, TestRunVerdict


T = TypeVar("T")


class TestCaseCreateRequest(TestCaseContent):
    """Command name over the canonical TestCase content fields."""


class TestPlanGenerateRequest(BaseModel):
    """HTTP 规划命令及临时事件关联；重新规划额外输入由服务验证。"""

    model_config = ConfigDict(extra="forbid")

    planning_request_id: PlanningRequestId
    user_input: str | None = None


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


class TestCaseListResponse(TestCase):
    """API list projection; the plan table owns the optional latest version."""

    latest_plan_version: int | None


class TestRunListResponse(TestRun):
    """API join projection, not a second persisted run entity.

    Plan/case tables own case identity; task definitions own task_count;
    TaskRun and screenshot rows own the counts. Never write this projection back.
    """

    test_case_id: TestCaseId
    test_case_name: str
    test_case_archived: bool
    task_count: int = Field(ge=0)
    task_status_counts: dict[TaskRunStatus, int]
    screenshot_count: int = Field(ge=0)


class TestRunFilterQuery(BaseModel):
    """Shared HTTP filter contract for the list and its unpaginated statistics."""

    model_config = ConfigDict(extra="forbid")

    search: str = ""
    test_case_id: TestCaseId | None = None
    status: list[TestRunStatus] = Field(default_factory=list)
    verdict: TestRunVerdict | None = None
    created_from: AwareDatetime | None = None
    created_before: AwareDatetime | None = None

    @model_validator(mode="after")
    def time_range_is_ordered(self) -> "TestRunFilterQuery":
        if self.created_from and self.created_before and self.created_from >= self.created_before:
            raise ValueError("created_from must be earlier than created_before")
        return self


class TestRunPageQuery(TestRunFilterQuery):
    """List-only pagination over the same filter facts."""

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class TestRunDailyStatistics(BaseModel):
    """Read-only aggregates grouped by creation day in Asia/Shanghai."""

    date: date
    run_count: int
    average_duration_seconds: float | None
    total_duration_seconds: float


class TestRunStatisticsResponse(BaseModel):
    """Whole-filter aggregates, independent of the visible table page."""

    run_count: int
    verdict_counts: dict[TestRunVerdict, int]
    average_duration_seconds: float | None
    daily: list[TestRunDailyStatistics]
