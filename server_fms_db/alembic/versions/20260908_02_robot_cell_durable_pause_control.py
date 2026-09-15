"""Add durable Robot Cell pause/resume control state.

Revision ID: 20260908_02
Revises: 20260908_01
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_02"
down_revision = "20260908_01"
branch_labels = None
depends_on = None


JOB_CONTROL = sa.Enum(
    "ACTIVE", "PAUSE_REQUESTED", "PAUSED", "RESUME_REQUESTED",
    name="production_job_control_state",
)
ATTEMPT_CONTROL = sa.Enum(
    "ACTIVE", "PAUSE_REQUESTED", "HELD", "RESUME_REQUESTED",
    name="execution_attempt_control_state",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        JOB_CONTROL.create(bind, checkfirst=True)
        ATTEMPT_CONTROL.create(bind, checkfirst=True)

    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.add_column(sa.Column("control_state", JOB_CONTROL, nullable=False, server_default="ACTIVE"))
        batch_op.add_column(sa.Column("control_req_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("control_requested_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("execution_attempts") as batch_op:
        batch_op.add_column(sa.Column("control_state", ATTEMPT_CONTROL, nullable=False, server_default="ACTIVE"))
        batch_op.add_column(sa.Column("control_req_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("control_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("control_dispatched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_attempts") as batch_op:
        batch_op.drop_column("control_dispatched_at")
        batch_op.drop_column("control_requested_at")
        batch_op.drop_column("control_req_id")
        batch_op.drop_column("control_state")
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.drop_column("control_requested_at")
        batch_op.drop_column("control_req_id")
        batch_op.drop_column("control_state")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        ATTEMPT_CONTROL.drop(bind, checkfirst=True)
        JOB_CONTROL.drop(bind, checkfirst=True)
