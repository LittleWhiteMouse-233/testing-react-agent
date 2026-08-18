from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ids import DeviceId, TestPlanId
from app.domain.planning import TestCaseContent, TestPlanContent, TestTaskDefinition
from app.domain.resources.device import DeviceHealth, DeviceInfo
from app.domain.resources.tools import ToolCatalogSnapshot


T = TypeVar("T")


class TestCaseCreateRequest(TestCaseContent):
    """Command name over the canonical TestCase content fields."""


class GeneratePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: DeviceId


class ReviseTestPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: TestPlanContent[TestTaskDefinition]


class TestRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_plan_id: TestPlanId
    device_id: DeviceId
    assumptions_confirmed: Literal[True]


class ReportExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["json", "html"]


class DeviceResponse(BaseModel):
    """HTTP 设备查询响应；由 route 从实时设备和工具 provider 投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    provider: str
    health: DeviceHealth
    info: DeviceInfo | None
    tool_catalog: ToolCatalogSnapshot | None


class PageResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    items: list[T]
    total: int = Field(ge=0)


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
