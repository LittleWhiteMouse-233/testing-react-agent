from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskType(StrEnum):
    ACT = "act"
    JUDGE = "judge"


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
