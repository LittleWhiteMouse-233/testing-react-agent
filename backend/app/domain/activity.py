"""模型与提示词共用的 planning/execution 活动词汇。"""

from enum import StrEnum


class AgentActivity(StrEnum):
    """Agent 正在执行的稳定活动，用于模型路由与提示词选择。"""

    PLANNING = "planning"
    EXECUTION = "execution"
