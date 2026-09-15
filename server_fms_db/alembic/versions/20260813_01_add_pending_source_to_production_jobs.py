"""Add pending request trace fields to production jobs.

Revision ID: 20260813_01
Revises: 20260812_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260813_01"
down_revision = "20260812_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("production_jobs", sa.Column("source_pending_request_id", sa.Integer(), nullable=True))
    op.add_column("production_jobs", sa.Column("source_item_index", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_production_jobs_source_pending_request",
        "production_jobs",
        "pending_production_requests",
        ["source_pending_request_id"],
        ["request_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_production_jobs_pending_source_pair",
        "production_jobs",
        "(source_pending_request_id IS NULL AND source_item_index IS NULL) "
        "OR (source_pending_request_id IS NOT NULL AND source_item_index IS NOT NULL AND source_item_index >= 1)",
    )
    op.create_unique_constraint(
        "uq_production_jobs_pending_source_item",
        "production_jobs",
        ["source_pending_request_id", "source_item_index"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_production_jobs_pending_source_item", "production_jobs", type_="unique")
    op.drop_constraint("ck_production_jobs_pending_source_pair", "production_jobs", type_="check")
    op.drop_constraint("fk_production_jobs_source_pending_request", "production_jobs", type_="foreignkey")
    op.drop_column("production_jobs", "source_item_index")
    op.drop_column("production_jobs", "source_pending_request_id")
