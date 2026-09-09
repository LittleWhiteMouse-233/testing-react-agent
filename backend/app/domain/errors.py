"""可跨应用边界传播的异常类型；持久化结果原因属于 execution domain。"""

from __future__ import annotations


class ModelCallTimeout(TimeoutError):
    """由 LLM 调用边界产生、由 Graph 重试并最终由执行器归因的超时。"""


class PlanningFailure(RuntimeError):
    """由 PlanningGraph 在有限重试耗尽后产生，供应用/API 边界消费。"""


class PromptVersionMismatch(RuntimeError):
    """由执行 factory 在可用 prompt 与 TestRun snapshot 不一致时产生。"""

    def __init__(self, *, prompt_name: str, expected: str, actual: str) -> None:
        super().__init__(
            f"Prompt {prompt_name} version mismatch: expected {expected}, got {actual}"
        )


class ModelProfileMismatch(RuntimeError):
    """由执行 factory 在 profile ID 对应配置与 TestRun snapshot 漂移时产生。"""


class ModelContextBudgetExceeded(RuntimeError):
    """由 TaskAgent 在固定工具/最新观察无法装入模型窗口时产生。"""
