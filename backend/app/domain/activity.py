from enum import StrEnum

from app.domain.planning import TestTaskType


class AgentActivity(StrEnum):
    PLANNING = "planning"
    ACT = "act"
    JUDGE = "judge"


def activity_for_task(task_type: TestTaskType) -> AgentActivity:
    return AgentActivity(task_type.value)
