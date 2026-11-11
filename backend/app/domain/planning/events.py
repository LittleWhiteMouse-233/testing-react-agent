"""临时规划进度契约；服务拥有请求归属，Graph 产生决策与工具事实。"""

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


class PlanningCycleEvent(PlanningEventBase):
    """实际决策轮次开始或有效计划提交，额度只用于进度。"""

    type: Literal["planning.cycle_started", "planning.validated"]
    cycle_count: int = Field(ge=1)
    max_cycles: int = Field(ge=1)


class PlanningResponseInvalidEvent(PlanningEventBase):
    """模型未给出一个可用工具调用，应用反馈后继续决策。"""

    type: Literal["planning.response_invalid"] = "planning.response_invalid"
    cycle_count: int = Field(ge=1)
    reason: str = Field(min_length=1)


class PlanningToolEventBase(PlanningEventBase):
    """调用开始和结果事件共用的调用身份及所属决策轮次。"""

    cycle_count: int = Field(ge=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)


class PlanningToolStartedEvent(PlanningToolEventBase):
    """工具实际开始的独立 SSE 边界，尚无结果状态。"""

    type: Literal["planning.tool_started"] = "planning.tool_started"


class PlanningToolCompletedEvent(PlanningToolEventBase):
    """调用结果 SSE 边界，在同一调用身份上增加实际状态。"""

    type: Literal["planning.tool_completed"] = "planning.tool_completed"
    status: Literal["success", "error"]
    reason: str | None = None


class PlanningFailedEvent(PlanningEventBase):
    """规划请求失败；原因也可能来自输入准备或计划保存。"""

    type: Literal["planning.failed"] = "planning.failed"
    reason: str = Field(min_length=1)


type PlanningEvent = Annotated[
    PlanningStageEvent | PlanningCycleEvent | PlanningResponseInvalidEvent | PlanningToolStartedEvent | PlanningToolCompletedEvent | PlanningFailedEvent,
    Field(discriminator="type"),
]

PLANNING_EVENT_ADAPTER = TypeAdapter(PlanningEvent)
