"""跨领域信息家族共享的强约束业务 ID 别名。"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints


UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

TestCaseId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
TestPlanId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
TestTaskId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
TestRunId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
TaskRunId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
RunEventId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
ArtifactId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]

DeviceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ModelProfileId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ToolCallId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
MessageId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
