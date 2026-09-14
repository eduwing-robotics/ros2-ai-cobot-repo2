"""Add PRE-ROOF inspection and roof-installation lifecycle runtime tables.

Revision ID: 20260813_04
Revises: 20260813_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_04"
down_revision = "20260813_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision deliberately uses only literal historical enum values.
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'ROOF_READY'")
    for value in (
        "PRE_ROOF_READY",
        "PRE_ROOF_INSPECTION_STARTED",
        "PRE_ROOF_INSPECTION_PASSED",
        "PRE_ROOF_INSPECTION_FAILED",
        "ROOF_INSTALLATION_STARTED",
        "ROOF_INSTALLATION_COMPLETED",
        "ROOF_INSTALLATION_FAILED",
    ):
        op.execute(f"ALTER TYPE production_event_type ADD VALUE IF NOT EXISTS '{value}'")

    inspection_type = postgresql.ENUM("PRE_ROOF", name="production_inspection_type")
    inspection_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "PASSED", "FAILED", name="production_inspection_status"
    )
    roof_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", name="roof_installation_status"
    )
    inspection_type.create(op.get_bind(), checkfirst=True)
    inspection_status.create(op.get_bind(), checkfirst=True)
    roof_status.create(op.get_bind(), checkfirst=True)
    inspection_type = postgresql.ENUM("PRE_ROOF", name="production_inspection_type", create_type=False)
    inspection_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "PASSED", "FAILED", name="production_inspection_status", create_type=False
    )
    roof_status = postgresql.ENUM(
        "PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", name="roof_installation_status", create_type=False
    )
    roof_option = postgresql.ENUM("ROOF_01", "ROOF_02", name="roof_option_code", create_type=False)

    op.create_table(
        "production_inspections",
        sa.Column("inspection_id", sa.Integer(), primary_key=True),
        sa.Column("production_job_id", sa.Integer(), sa.ForeignKey("production_jobs.job_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("inspection_type", inspection_type, nullable=False),
        sa.Column("status", inspection_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("production_job_id", "inspection_type", name="uq_production_inspections_job_type"),
    )
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


def downgrade() -> None:
    op.drop_table("roof_installations")
    op.drop_table("production_inspections")
    postgresql.ENUM(name="roof_installation_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="production_inspection_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="production_inspection_type").drop(op.get_bind(), checkfirst=True)
    # PostgreSQL enum values added to job_status and production_event_type are
    # intentionally retained: removing them requires destructive enum recreation.
