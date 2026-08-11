from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.artifacts import Artifact
from app.domain.device import DeviceHealth, DeviceInfo
from app.domain.events import StoredRunEvent
from app.domain.execution import TaskRun, TestRun, TestRunSnapshot
from app.domain.ids import DeviceId
from app.domain.planning import TestPlan
from app.domain.tools import ToolCatalogSnapshot


class TestRunDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: TestRun
    test_plan: TestPlan
    snapshot: TestRunSnapshot
    task_runs: list[TaskRun]


class TestRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: TestRunDetail
    events: list[StoredRunEvent]
    artifacts: list[Artifact]


class DeviceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    provider: str
    health: DeviceHealth
    info: DeviceInfo | None
    tool_catalog: ToolCatalogSnapshot | None
