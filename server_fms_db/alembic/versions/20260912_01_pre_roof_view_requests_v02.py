"""Persist PRE_ROOF v0.2 server-owned per-view request history.

Revision ID: 20260912_01
Revises: 20260910_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260912_01"
down_revision = "20260910_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This enum was created by the PRE_ROOF foundation migration.  Use the
    # PostgreSQL dialect type explicitly: generic ``sa.Enum`` does not own the
    # dialect-level ``create_type`` switch during ``op.create_table``.
    result_enum = postgresql.ENUM(
        "PASS", "FAIL", "NOT_EVALUATED",
        name="production_inspection_result",
        create_type=False,
    )
    op.create_table(
        "production_inspection_view_requests",
        sa.Column("view_request_id", sa.Integer(), primary_key=True),
        sa.Column("inspection_id", sa.Integer(), sa.ForeignKey("production_inspections.inspection_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("view_name", sa.String(length=10), nullable=False),
        sa.Column("inspection_request_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="REQUESTED"),
        sa.Column("result", result_enum, nullable=True),
        sa.Column("request_snapshot_json", sa.Text(), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runtime_version", sa.String(length=100), nullable=True),
        sa.Column("runtime_port", sa.Integer(), nullable=True),
        sa.Column("vision_production_valid", sa.Boolean(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("inspection_request_id", name="uq_pre_roof_view_requests_request_id"),
        sa.CheckConstraint("view_name IN ('TOP', 'LEFT', 'RIGHT', 'FRONT', 'BEHIND')", name="ck_pre_roof_view_requests_view_name"),
        sa.CheckConstraint("status IN ('REQUESTED', 'SENT', 'ACKED', 'COMPLETED', 'FAILED')", name="ck_pre_roof_view_requests_status"),
        sa.CheckConstraint("retry_count >= 0", name="ck_pre_roof_view_requests_retry_count"),
    )


def downgrade() -> None:
    op.drop_table("production_inspection_view_requests")
