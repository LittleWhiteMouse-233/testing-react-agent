"""跨 planning、execution 与资源路由共享的 Agent 活动词汇。"""

from enum import StrEnum


class AgentActivity(StrEnum):
    """Agent 正在执行的稳定活动，用于模型路由和工具授权。"""

    PLANNING = "planning"
    ACT = "act"
    JUDGE = "judge"
