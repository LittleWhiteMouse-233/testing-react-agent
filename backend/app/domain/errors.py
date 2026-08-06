from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    COMPLETED = "completed"
    ASSERTION_FAILED = "assertion_failed"
    GOAL_UNREACHABLE = "goal_unreachable"
    CYCLE_LIMIT = "cycle_limit"
    DEVICE_UNAVAILABLE = "device_unavailable"
    CAPTURE_FAILED = "capture_failed"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    AGENT_BLOCKED = "agent_blocked"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TOOL_FAILED = "tool_failed"
    PROCESS_RESTARTED = "process_restarted"
    GLOBAL_FAIL_FAST = "global_fail_fast"
    USER_CANCELLED = "user_cancelled"
    UNEXPECTED_ERROR = "unexpected_error"


class DeviceUnavailable(RuntimeError):
    pass


class CaptureFailed(RuntimeError):
    pass


class ActionTimeout(RuntimeError):
    pass


class UnsupportedAction(ValueError):
    pass


class PlanningFailure(RuntimeError):
    pass
