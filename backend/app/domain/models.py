from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class OverallResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TaskType(StrEnum):
    ACT = "act"
    JUDGE = "judge"


class RemoteKey(StrEnum):
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"
    DPAD_CENTER = "DPAD_CENTER"
    BACK = "BACK"
    HOME = "HOME"
    MENU = "MENU"
    POWER = "POWER"
    VOLUME_UP = "VOLUME_UP"
    VOLUME_DOWN = "VOLUME_DOWN"
    MUTE = "MUTE"
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    STOP = "STOP"
    NEXT = "NEXT"
    PREVIOUS = "PREVIOUS"
    TAB = "TAB"
    ENTER = "ENTER"
    DEL = "DEL"
    DIGIT_0 = "DIGIT_0"
    DIGIT_1 = "DIGIT_1"
    DIGIT_2 = "DIGIT_2"
    DIGIT_3 = "DIGIT_3"
    DIGIT_4 = "DIGIT_4"
    DIGIT_5 = "DIGIT_5"
    DIGIT_6 = "DIGIT_6"
    DIGIT_7 = "DIGIT_7"
    DIGIT_8 = "DIGIT_8"
    DIGIT_9 = "DIGIT_9"


class DeviceProfile(BaseModel):
    model: str | None = None
    resolution: str | None = None
    locale: str | None = None


class PlanRequest(BaseModel):
    test_case_id: str
    text: str
    device_profile: DeviceProfile | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=100)
    type: TaskType
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    max_cycles: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def criteria_are_not_blank(self) -> "Task":
        if any(not item.strip() for item in self.success_criteria):
            raise ValueError("success_criteria must not contain blank values")
        return self


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    setup_steps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def task_ids_are_unique(self) -> "PlanOutput":
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id values must be unique")
        if any(not item.strip() for item in self.setup_steps + self.assumptions):
            raise ValueError("setup_steps and assumptions must not contain blank values")
        return self


class PressKeyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["PRESS_KEY"]
    key: RemoteKey


class InputTextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["INPUT_TEXT"]
    text: str = Field(min_length=1, max_length=1000)


class WaitAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["WAIT"]
    duration_ms: int = Field(ge=100, le=10_000)


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["TOOL"]
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


Action = Annotated[
    PressKeyAction | InputTextAction | WaitAction | ToolAction,
    Field(discriminator="type"),
]


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["action"]
    summary: str = Field(min_length=1)
    action: Action


class TaskPassDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["task_pass"]
    summary: str = Field(min_length=1)
    evidence_artifact_ids: list[str] = Field(min_length=1)


class TaskFailDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["task_fail"]
    summary: str = Field(min_length=1)
    evidence_artifact_ids: list[str] = Field(min_length=1)


class BlockedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["blocked"]
    summary: str = Field(min_length=1)
    evidence_artifact_ids: list[str] = Field(default_factory=list)


Decision = Annotated[
    ActionDecision | TaskPassDecision | TaskFailDecision | BlockedDecision,
    Field(discriminator="type"),
]
DECISION_ADAPTER = TypeAdapter(Decision)


class DeviceHealth(BaseModel):
    available: bool
    message: str = ""


class DeviceCapabilities(BaseModel):
    screenshot: bool = True
    input_text: bool = True
    supported_keys: list[RemoteKey] = Field(default_factory=lambda: list(RemoteKey))
    model: str | None = None
    resolution: str | None = None
    locale: str | None = None


class ScreenshotData(BaseModel):
    content: bytes
    mime_type: str = "image/png"
    activity: str | None = None


class ActionResult(BaseModel):
    success: bool = True
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    changes_device_state: bool = False
    timeout_seconds: float = 15
    source: str = "builtin"


class ToolContext(BaseModel):
    run_id: str
    task_run_id: str | None = None
    device_id: str


class ToolResult(BaseModel):
    success: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class DecisionContext(BaseModel):
    task: Task
    artifact_id: str
    screenshot_bytes: bytes
    screenshot_mime_type: str
    capabilities: DeviceCapabilities
    tools: list[ToolDefinition]
    recent_history: list[dict[str, Any]]
    cycle_count: int
    max_cycles: int


class DeviceUnavailable(RuntimeError):
    pass


class CaptureFailed(RuntimeError):
    pass


class ActionTimeout(RuntimeError):
    pass


class UnsupportedAction(ValueError):
    pass


class InvalidArguments(ValueError):
    pass


class TransientToolError(RuntimeError):
    pass

