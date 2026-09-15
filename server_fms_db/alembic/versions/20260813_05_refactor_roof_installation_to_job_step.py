"""Refactor roof installation runtime state into JobStep.

Revision ID: 20260813_05
Revises: 20260813_04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_05"
down_revision = "20260813_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision is independent of application models and enum definitions.
    roof_option = postgresql.ENUM("ROOF_01", "ROOF_02", name="roof_option_code", create_type=False)
    op.add_column("job_steps", sa.Column("roof_option_code", roof_option, nullable=True))
    op.create_check_constraint(
        "ck_job_steps_roof_snapshot",
        "job_steps",
        "roof_option_code IS NULL OR (source_recipe_stage_id IS NULL AND process_step_id IS NULL AND operation_code = 'INSTALL_ROOF')",
    )
    op.create_index(
        "uq_job_steps_runtime_roof",
        "job_steps",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("operation_code = 'INSTALL_ROOF'"),
    )

    op.drop_table("roof_installations")
    postgresql.ENUM(name="roof_installation_status").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    roof_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", name="roof_installation_status"
    )
    roof_status.create(op.get_bind(), checkfirst=True)
    roof_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", name="roof_installation_status", create_type=False
    )
    roof_option = postgresql.ENUM("ROOF_01", "ROOF_02", name="roof_option_code", create_type=False)
    op.create_table(
        "roof_installations",
        sa.Column("roof_installation_id", sa.Integer(), primary_key=True),
        sa.Column("production_job_id", sa.Integer(), sa.ForeignKey("production_jobs.job_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("roof_option_code", roof_option, nullable=False),
        sa.Column("status", roof_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("production_job_id", name="uq_roof_installations_job"),
    )

    op.drop_index("uq_job_steps_runtime_roof", table_name="job_steps")
    op.drop_constraint("ck_job_steps_roof_snapshot", "job_steps", type_="check")
    op.drop_column("job_steps", "roof_option_code")
