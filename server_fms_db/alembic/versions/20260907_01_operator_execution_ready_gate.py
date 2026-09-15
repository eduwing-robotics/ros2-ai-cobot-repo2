"""Add durable operator release before transported Robot Cell execution.

Revision ID: 20260907_01
Revises: 20260905_02
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "20260907_01"
down_revision = "20260905_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_steps",
        sa.Column("operator_execution_ready_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_steps", "operator_execution_ready_at")
