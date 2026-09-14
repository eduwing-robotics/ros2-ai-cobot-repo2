"""Allow a pre-materialized deferred DeliveryItem before its runtime JobStep.

Revision ID: 20260903_01
Revises: 20260902_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_01"
down_revision = "20260902_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "job_material_delivery_items",
        "job_step_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text("SELECT count(*) FROM job_material_delivery_items WHERE job_step_id IS NULL")
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "Cannot downgrade 20260903_01 while deferred DeliveryItems have NULL job_step_id."
        )
    op.alter_column(
        "job_material_delivery_items",
        "job_step_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
