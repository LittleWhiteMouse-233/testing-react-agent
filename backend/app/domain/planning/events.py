"""临时规划进度契约；请求归属由服务拥有，Graph 仅产生尝试进度。"""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.domain.ids import PlanningRequestId, TestCaseId


class PlanningEventBase(BaseModel):
    """服务拥有的单次规划请求归属及事件发生时间。"""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)

    test_case_id: TestCaseId
    planning_request_id: PlanningRequestId
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlanningStageEvent(PlanningEventBase):
    """输入准备与持久化阶段；不复制计划内容或数据库版本事实。"""

    type: Literal["planning.started", "planning.input_ready", "planning.saving", "planning.succeeded"]


class PlanningAttemptEvent(PlanningEventBase):
    """模型尝试开始或校验完成，需要明确本次尝试序号。"""

    type: Literal["planning.attempt_started", "planning.validated"]
    attempt: int = Field(ge=1, le=3)


class PlanningAttemptFailedEvent(PlanningEventBase):
    """可重试的单次模型失败，与整个请求失败具有不同生命周期。"""

    type: Literal["planning.attempt_failed"] = "planning.attempt_failed"
    attempt: int = Field(ge=1, le=3)
    reason: str = Field(min_length=1)


class PlanningFailedEvent(PlanningEventBase):
    """规划请求失败；原因也可能来自输入准备或计划保存。"""

    type: Literal["planning.failed"] = "planning.failed"
    reason: str = Field(min_length=1)


type PlanningEvent = Annotated[
    PlanningStageEvent | PlanningAttemptEvent | PlanningAttemptFailedEvent | PlanningFailedEvent,
    Field(discriminator="type"),
]

PLANNING_EVENT_ADAPTER = TypeAdapter(PlanningEvent)
