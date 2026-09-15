"""Add durable MANUAL prestage evidence for job material Deliveries.

Revision ID: 20260902_01
Revises: 20260901_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260902_01"
down_revision = "20260901_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_material_deliveries",
        sa.Column("manual_prestage_ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_material_deliveries",
        sa.Column("manual_prestage_request_id", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_material_deliveries", "manual_prestage_request_id")
    op.drop_column("job_material_deliveries", "manual_prestage_ready_at")
