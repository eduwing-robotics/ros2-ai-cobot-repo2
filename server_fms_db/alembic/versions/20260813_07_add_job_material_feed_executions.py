"""Add durable Material Feed runtime execution per job delivery batch.

Revision ID: 20260813_07
Revises: 20260813_06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260813_07"
down_revision = "20260813_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    feed_status = postgresql.ENUM("PENDING", "RUNNING", "COMPLETED", "FAILED", name="job_material_feed_status")
    feed_status.create(op.get_bind(), checkfirst=True)
    feed_status = postgresql.ENUM("PENDING", "RUNNING", "COMPLETED", "FAILED", name="job_material_feed_status", create_type=False)
    op.create_table(
        "job_material_feed_executions",
        sa.Column("feed_execution_id", sa.Integer(), primary_key=True),
        sa.Column("job_delivery_id", sa.Integer(), sa.ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", feed_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("completed_json", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        sa.UniqueConstraint("job_delivery_id", name="uq_job_material_feed_executions_delivery"),
    )
    # Existing delivery history cannot prove Cell feed completion; every row begins PENDING.
    op.execute("""
        INSERT INTO job_material_feed_executions (job_delivery_id, status, completed_json)
        SELECT job_delivery_id, 'PENDING', '[]' FROM job_material_deliveries
    """)


def downgrade() -> None:
    op.drop_table("job_material_feed_executions")
    postgresql.ENUM(name="job_material_feed_status").drop(op.get_bind(), checkfirst=True)
