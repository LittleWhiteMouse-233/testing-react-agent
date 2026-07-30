from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TestCaseRow(Base):
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PlanRevisionRow(Base):
    __tablename__ = "plan_revisions"
    __table_args__ = (
        UniqueConstraint("test_case_id", "revision", name="uq_plan_revision_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_case_id: Mapped[str] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL"), nullable=True
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    model_info_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestRunRow(Base):
    __tablename__ = "test_runs"
    __table_args__ = (
        Index("ix_single_active_run", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_case_id: Mapped[str] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT"), index=True
    )
    plan_revision_id: Mapped[str] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="RESTRICT"), index=True
    )
    device_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), index=True)
    overall_result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRunRow(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("test_run_id", "task_index", name="uq_task_run_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), index=True
    )
    task_index: Mapped[int] = mapped_column(Integer)
    task_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    outcome_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepEventRow(Base):
    __tablename__ = "step_events"
    __table_args__ = (
        UniqueConstraint("test_run_id", "sequence", name="uq_event_sequence"),
        UniqueConstraint("test_run_id", "dedup_key", name="uq_event_dedup"),
        Index("ix_events_run_sequence", "test_run_id", "sequence"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_run_id: Mapped[str] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), index=True
    )
    task_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(30))
    relative_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
