"""Persist immediate-stop intent for durable Robot Cell pause requests.

Revision ID: 20260910_01
Revises: 20260908_02
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260910_01"
down_revision = "20260908_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The server default safely initializes existing durable pause rows to the
    # non-immediate behavior. Runtime writes the value for every new request.
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "control_immediate_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.drop_column("control_immediate_requested")
