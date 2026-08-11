from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import ArtifactId, TaskRunId, TestRunId


class ArtifactType(StrEnum):
    SCREENSHOT = "screenshot"
    JSON_EXPORT = "json_export"
    HTML_EXPORT = "html_export"


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: ArtifactId
    test_run_id: TestRunId | None
    task_run_id: TaskRunId | None
    type: ArtifactType
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def owner_is_exclusive(self) -> "Artifact":
        if (self.test_run_id is None) == (self.task_run_id is None):
            raise ValueError("artifact requires exactly one direct owner")
        if self.type == ArtifactType.SCREENSHOT and self.task_run_id is None:
            raise ValueError("screenshot artifacts must belong to a task run")
        if self.type != ArtifactType.SCREENSHOT and self.test_run_id is None:
            raise ValueError("export artifacts must belong to a test run")
        return self
