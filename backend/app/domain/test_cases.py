from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ids import TestCaseId


class TestCaseContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def text_is_not_blank(self) -> "TestCaseContent":
        if not self.name.strip() or not self.source_text.strip():
            raise ValueError("test case name and source_text must not be blank")
        return self


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: TestCaseId
    content: TestCaseContent
    created_at: datetime
