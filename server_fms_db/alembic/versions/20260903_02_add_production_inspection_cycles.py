"""Preserve PRE_ROOF inspection history with explicit cycles.

Revision ID: 20260903_02
Revises: 20260903_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_02"
down_revision = "20260903_01"
branch_labels = None
depends_on = None


OLD_UNIQUE = "uq_production_inspections_job_type"
NEW_UNIQUE = "uq_production_inspections_job_type_cycle"
CYCLE_CHECK = "ck_production_inspections_cycle_positive"


def upgrade() -> None:
    op.add_column(
        "production_inspections",
        sa.Column("inspection_cycle", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE production_inspections SET inspection_cycle = 1 "
        "WHERE inspection_cycle IS NULL"
    )
    op.alter_column(
        "production_inspections",
        "inspection_cycle",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_check_constraint(
        CYCLE_CHECK,
        "production_inspections",
        "inspection_cycle >= 1",
    )
    op.drop_constraint(OLD_UNIQUE, "production_inspections", type_="unique")
    op.create_unique_constraint(
        NEW_UNIQUE,
        "production_inspections",
        ["production_job_id", "inspection_type", "inspection_cycle"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    multi_cycle_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "SELECT production_job_id, inspection_type "
            "FROM production_inspections "
            "GROUP BY production_job_id, inspection_type "
            "HAVING count(*) > 1"
            ") AS multi_cycle"
        )
    ).scalar_one()
    if multi_cycle_count:
        raise RuntimeError(
            "Cannot downgrade 20260903_02 while ProductionInspection history has multiple cycles."
        )
    op.drop_constraint(NEW_UNIQUE, "production_inspections", type_="unique")
    op.drop_constraint(CYCLE_CHECK, "production_inspections", type_="check")
    op.drop_column("production_inspections", "inspection_cycle")
    op.create_unique_constraint(
        OLD_UNIQUE,
        "production_inspections",
        ["production_job_id", "inspection_type"],
    )
