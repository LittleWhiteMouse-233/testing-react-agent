from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.execution import ArtifactType
from app.domain.execution import TaskRunStatus, TestRunStatus, TestRunVerdict
from app.domain.planning import TestPlanOrigin, TestTaskType
from app.persistence.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_column(enum_type: type, name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda values: [item.value for item in values],
    )


class TestCaseRow(Base):
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestPlanRow(Base):
    __tablename__ = "test_plans"
    __table_args__ = (
        UniqueConstraint(
            "test_case_id", "version_number", name="uq_test_plan_version"
        ),
        CheckConstraint(
            "(origin = 'manual_revision' AND derived_from_plan_id IS NOT NULL) "
            "OR (origin != 'manual_revision' AND derived_from_plan_id IS NULL)",
            name="ck_test_plan_lineage",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_case_id: Mapped[str] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    origin: Mapped[TestPlanOrigin] = mapped_column(
        enum_column(TestPlanOrigin, "test_plan_origin")
    )
    derived_from_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_plans.id", ondelete="RESTRICT"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200))
    setup_steps_json: Mapped[list[str]] = mapped_column(JSON)
    assumptions_json: Mapped[list[str]] = mapped_column(JSON)
    planning_context_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestTaskRow(Base):
    __tablename__ = "test_tasks"
    __table_args__ = (
        UniqueConstraint("test_plan_id", "position", name="uq_test_task_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_plan_id: Mapped[str] = mapped_column(
        ForeignKey("test_plans.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[TestTaskType] = mapped_column(
        enum_column(TestTaskType, "test_task_type")
    )
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    success_criteria_json: Mapped[list[str]] = mapped_column(JSON)
    max_cycles: Mapped[int] = mapped_column(Integer)


class TestRunRow(Base):
    __tablename__ = "test_runs"
    __table_args__ = (
        Index(
            "uq_single_active_run",
            text("(1)"),
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_plan_id: Mapped[str] = mapped_column(
        ForeignKey("test_plans.id", ondelete="RESTRICT"), index=True
    )
    device_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[TestRunStatus] = mapped_column(
        enum_column(TestRunStatus, "test_run_status"), index=True
    )
    verdict: Mapped[TestRunVerdict | None] = mapped_column(
        enum_column(TestRunVerdict, "test_run_verdict"), nullable=True
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class TaskRunRow(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint(
            "test_run_id", "test_task_id", name="uq_task_run_test_task"
        ),
        CheckConstraint("cycle_count >= 0", name="ck_task_run_cycle_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), index=True
    )
    test_task_id: Mapped[str] = mapped_column(
        ForeignKey("test_tasks.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[TaskRunStatus] = mapped_column(
        enum_column(TaskRunStatus, "task_run_status")
    )
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("test_run_id", "sequence", name="uq_run_event_sequence"),
        UniqueConstraint("test_run_id", "dedup_key", name="uq_run_event_dedup"),
        Index("ix_run_events_run_sequence", "test_run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE")
    )
    task_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(50))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "(test_run_id IS NULL) != (task_run_id IS NULL)",
            name="ck_artifact_owner_xor",
        ),
        CheckConstraint(
            "(type = 'screenshot' AND task_run_id IS NOT NULL) OR "
            "(type IN ('json_export', 'html_export') AND test_run_id IS NOT NULL)",
            name="ck_artifact_type_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    task_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    type: Mapped[ArtifactType] = mapped_column(
        enum_column(ArtifactType, "artifact_type")
    )
    relative_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
