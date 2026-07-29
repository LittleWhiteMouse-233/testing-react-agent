from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.models import DeviceProfile, PlanOutput


class TestCaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1)


class PlanCreate(BaseModel):
    device_profile: DeviceProfile | None = None


class PlanRevisionCreate(BaseModel):
    plan: PlanOutput


class RunCreate(BaseModel):
    plan_revision_id: str
    device_id: str
    confirmed_assumptions: list[str] = Field(default_factory=list)


class ExportCreate(BaseModel):
    format: Literal["json", "html"]

