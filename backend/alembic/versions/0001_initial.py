"""initial schema

Revision ID: 0001
Revises:
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_case_id", sa.String(36), sa.ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("parent_revision_id", sa.String(36), sa.ForeignKey("plan_revisions.id", ondelete="SET NULL")),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("model_info_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("test_case_id", "revision", name="uq_plan_revision_number"),
    )
    op.create_index("ix_plan_revisions_test_case_id", "plan_revisions", ["test_case_id"])
    op.create_table(
        "test_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_case_id", sa.String(36), sa.ForeignKey("test_cases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("plan_revision_id", sa.String(36), sa.ForeignKey("plan_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("device_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("overall_result", sa.String(20)),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_runs_test_case_id", "test_runs", ["test_case_id"])
    op.create_index("ix_test_runs_plan_revision_id", "test_runs", ["plan_revision_id"])
    op.create_index("ix_test_runs_status", "test_runs", ["status"])
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("task_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cycle_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("test_run_id", "task_index", name="uq_task_run_index"),
    )
    op.create_index("ix_task_runs_test_run_id", "task_runs", ["test_run_id"])
    op.create_table(
        "step_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_run_id", sa.String(36), sa.ForeignKey("task_runs.id", ondelete="CASCADE")),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("test_run_id", "sequence", name="uq_event_sequence"),
        sa.UniqueConstraint("test_run_id", "dedup_key", name="uq_event_dedup"),
    )
    op.create_index("ix_events_run_sequence", "step_events", ["test_run_id", "sequence"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_run_id", sa.String(36), sa.ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_run_id", sa.String(36), sa.ForeignKey("task_runs.id", ondelete="SET NULL")),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_test_run_id", "artifacts", ["test_run_id"])


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("step_events")
    op.drop_table("task_runs")
    op.drop_table("test_runs")
    op.drop_table("plan_revisions")
    op.drop_table("test_cases")

