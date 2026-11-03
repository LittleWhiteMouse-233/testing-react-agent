from __future__ import annotations

from datetime import date
from typing import Generic, Literal, TypeVar
from collections.abc import Sequence

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic_core import ErrorDetails

from app.config import LLMProfileSettings, SettingsContent, SettingsService

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

    test_case_id: TestCaseId
    test_plan_id: TestPlanId
    assumptions_confirmed: Literal[True]


class ReportExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["json", "html"]


class PageResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    total: int = Field(ge=0)


class ApiValidationError(BaseModel):
    """HTTP 字段校验错误；仅包含定位与安全说明，不携带输入。"""

    model_config = ConfigDict(extra="forbid")

    loc: list[str | int]
    msg: str


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    errors: list[ApiValidationError] | None = None


def api_validation_error(errors: Sequence[ErrorDetails], *, settings_request: bool = False) -> ApiError:
    """框架及完整配置校验共用同一公开错误投影。"""
    public_errors: list[ApiValidationError] = []
    for error in errors:
        location = list(error["loc"])
        if location[:1] == ["body"]:
            location = location[1:]
        if settings_request and location[:1] == ["settings"]:
            location = location[1:]
        public_errors.append(ApiValidationError(loc=location, msg=error["msg"]))
    return ApiError(code="validation_error", message="请求参数校验失败", errors=public_errors)


class LLMProfileSettingsResponse(LLMProfileSettings):
    """设置公开投影隐藏已有凭据，并说明是否已配置。"""

    api_key: str | None = Field(default=None, exclude=True, repr=False)
    api_key_configured: bool


class SettingsUpdateRequest(BaseModel):
    """携带乐观版本的完整设置替换命令；令牌为空时保留原值。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: str
    settings: SettingsContent[LLMProfileSettings]


class SettingsResponse(BaseModel):
    """设置页内容、渲染协议及进程级提示的统一响应。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    restart_required: bool
    settings: SettingsContent[LLMProfileSettingsResponse]
    settings_schema: dict[str, JsonValue]

    @classmethod
    def from_service(cls, service: SettingsService) -> SettingsResponse:
        """集中完成已有凭据不回显这一接口边界转换。"""
        configuration = service.current.model_dump()
        for profile in configuration["llm"]["profiles"]:
            profile["api_key_configured"] = bool(profile.pop("api_key"))
        return cls.model_validate({
            "version": service.version,
            "restart_required": service.restart_required,
            "settings": configuration,
            "settings_schema": SettingsContent[LLMProfileSettings].model_json_schema(),
        })


class TestCaseListResponse(TestCase):
    """API list projection; the plan table owns the optional latest version."""

    latest_plan_version: int | None


class TestRunListResponse(TestRun):
    """API join projection, not a second persisted run entity.

    Plan/case tables own case identity; task definitions own task_count;
    TaskRun and image rows own the counts. Never write this projection back.
    """

    test_case_id: TestCaseId
    test_case_name: str
    test_case_archived: bool
    task_count: int = Field(ge=0)
    task_status_counts: dict[TaskRunStatus, int]
    image_count: int = Field(ge=0)


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
