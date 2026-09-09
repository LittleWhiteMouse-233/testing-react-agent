"""模型客户端的 planning/execution 路由词汇。"""

from enum import StrEnum


class AgentActivity(StrEnum):
    """Agent 正在执行的稳定活动，仅用于模型路由。"""

    PLANNING = "planning"
    EXECUTION = "execution"
