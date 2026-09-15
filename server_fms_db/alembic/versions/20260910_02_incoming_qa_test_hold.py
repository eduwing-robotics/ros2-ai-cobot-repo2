"""Persist the per-job Incoming QA test hold.

Revision ID: 20260910_02
Revises: 20260910_01
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260910_02"
down_revision = "20260910_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "incoming_qa_test_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.drop_column("incoming_qa_test_hold")
