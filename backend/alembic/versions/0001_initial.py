"""canonical data-structure baseline

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


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "test_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "test_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "test_case_id",
            sa.String(36),
            sa.ForeignKey("test_cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "origin",
            _enum(
                "test_plan_origin", "planning", "replanning", "manual_revision"
            ),
            nullable=False,
        ),
        sa.Column(
            "derived_from_plan_id",
            sa.String(36),
            sa.ForeignKey("test_plans.id", ondelete="RESTRICT"),
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("setup_steps_json", sa.JSON(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("planning_context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "test_case_id", "version_number", name="uq_test_plan_version"
        ),
        sa.CheckConstraint(
            "(origin = 'manual_revision' AND derived_from_plan_id IS NOT NULL) "
            "OR (origin != 'manual_revision' AND derived_from_plan_id IS NULL)",
            name="ck_test_plan_lineage",
        ),
    )
    op.create_index("ix_test_plans_test_case_id", "test_plans", ["test_case_id"])
    op.create_table(
        "test_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "test_plan_id",
            sa.String(36),
            sa.ForeignKey("test_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type", _enum("test_task_type", "act", "judge"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("success_criteria_json", sa.JSON(), nullable=False),
        sa.Column("max_cycles", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "test_plan_id", "position", name="uq_test_task_position"
        ),
    )
    op.create_index("ix_test_tasks_test_plan_id", "test_tasks", ["test_plan_id"])
    op.create_table(
        "test_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "test_plan_id",
            sa.String(36),
            sa.ForeignKey("test_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(200), nullable=False),
        sa.Column(
            "status",
            _enum("test_run_status", "pending", "running", "finished"),
            nullable=False,
        ),
        sa.Column(
            "verdict",
            _enum(
                "test_run_verdict", "PASS", "FAIL", "BLOCKED", "CANCELLED"
            ),
        ),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_runs_test_plan_id", "test_runs", ["test_plan_id"])
    op.create_index("ix_test_runs_status", "test_runs", ["status"])
    op.create_index(
        "uq_single_active_run",
        "test_runs",
        [sa.literal_column("(1)")],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "test_run_id",
            sa.String(36),
            sa.ForeignKey("test_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "test_task_id",
            sa.String(36),
            sa.ForeignKey("test_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum(
                "task_run_status",
                "running",
                "passed",
                "failed",
                "blocked",
                "skipped",
                "cancelled",
            ),
            nullable=False,
        ),
        sa.Column("cycle_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "test_run_id", "test_task_id", name="uq_task_run_test_task"
        ),
        sa.CheckConstraint("cycle_count >= 0", name="ck_task_run_cycle_count"),
    )
    op.create_index("ix_task_runs_test_run_id", "task_runs", ["test_run_id"])
    op.create_index("ix_task_runs_test_task_id", "task_runs", ["test_task_id"])
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "test_run_id",
            sa.String(36),
            sa.ForeignKey("test_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_run_id",
            sa.String(36),
            sa.ForeignKey("task_runs.id", ondelete="CASCADE"),
        ),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "test_run_id", "sequence", name="uq_run_event_sequence"
        ),
        sa.UniqueConstraint("test_run_id", "dedup_key", name="uq_run_event_dedup"),
    )
    op.create_index(
        "ix_run_events_run_sequence", "run_events", ["test_run_id", "sequence"]
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "test_run_id",
            sa.String(36),
            sa.ForeignKey("test_runs.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "task_run_id",
            sa.String(36),
            sa.ForeignKey("task_runs.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "type",
            _enum("artifact_type", "screenshot", "json_export", "html_export"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(test_run_id IS NULL) != (task_run_id IS NULL)",
            name="ck_artifact_owner_xor",
        ),
        sa.CheckConstraint(
            "(type = 'screenshot' AND task_run_id IS NOT NULL) OR "
            "(type IN ('json_export', 'html_export') AND test_run_id IS NOT NULL)",
            name="ck_artifact_type_owner",
        ),
    )
    op.create_index("ix_artifacts_test_run_id", "artifacts", ["test_run_id"])
    op.create_index("ix_artifacts_task_run_id", "artifacts", ["task_run_id"])


def downgrade() -> None:
    for table in (
        "artifacts",
        "run_events",
        "task_runs",
        "test_runs",
        "test_tasks",
        "test_plans",
        "test_cases",
    ):
        op.drop_table(table)
