"""可跨应用边界传播的异常类型；持久化结果原因属于 execution domain。"""

from __future__ import annotations

import sys
from traceback import TracebackException


class ToolCallError(RuntimeError):
    """The tool invocation mechanism or its result processing failed."""


def describe_exception(exception: BaseException, *, phase: str) -> str:
    """Project complete causes/groups to user messages without stack frames or locals."""
    diagnostic = TracebackException.from_exception(
        exception, limit=0, capture_locals=False,
        max_group_width=sys.maxsize, max_group_depth=sys.maxsize,
    )
    return f"{phase}: {''.join(diagnostic.format(chain=True)).strip()}"


class ModelCallTimeout(TimeoutError):
    """由 LLM 调用边界产生、由 Graph 重试并最终由执行器归因的超时。"""


class PlanningFailure(RuntimeError):
    """由 PlanningGraph 在有限重试耗尽后产生，供应用/API 边界消费。"""


class PlanningUserInputRequired(ValueError):
    """重新规划缺少本轮用户修改要求，必须在模型调用前拒绝。"""


class TestPlanNotLatest(ValueError):
    """计划创建事务发现本次参考版本已不是最新版本。"""


class TestCasePlanning(RuntimeError):
    """用例仍在生成计划，暂不允许修改或归档。"""


class TestCaseBusy(RuntimeError):
    """用例已被不兼容的操作占用，调用者应等待该操作结束。"""


class TestCaseHasActiveRun(RuntimeError):
    """用例关联待执行或运行中的记录，暂不允许归档。"""


class ModelContextBudgetExceeded(RuntimeError):
    """由 ModelContextWindow 在必要上下文或固定工具无法装入模型输入预算时产生。"""
