"""Link layout inspection results to their inspection execution.

Revision ID: 20260813_06
Revises: 20260813_05
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_06"
down_revision = "20260813_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision deliberately uses literal SQL only; it imports no ORM models.
    op.add_column("inspection_results", sa.Column("inspection_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_inspection_results_inspection",
        "inspection_results",
        "production_inspections",
        ["inspection_id"],
        ["inspection_id"],
        ondelete="RESTRICT",
    )

    # Historical rows are mapped only when the same Job has exactly one PRE_ROOF
    # execution. Missing or ambiguous history is intentionally left untouched.
    op.execute(
        """
        WITH unique_pre_roof_inspections AS (
            SELECT production_job_id, min(inspection_id) AS inspection_id
            FROM production_inspections
            WHERE inspection_type = 'PRE_ROOF'
            GROUP BY production_job_id
            HAVING count(*) = 1
        )
        UPDATE inspection_results AS result
        SET inspection_id = inspection.inspection_id
        FROM unique_pre_roof_inspections AS inspection
        WHERE result.job_id = inspection.production_job_id
          AND result.inspection_id IS NULL
        """
    )

    bind = op.get_bind()
    unmapped_count = bind.execute(
        sa.text("SELECT count(*) FROM inspection_results WHERE inspection_id IS NULL")
    ).scalar_one()
    if unmapped_count:
        raise RuntimeError(
            "inspection_results historical rows could not be mapped uniquely to "
            "production_inspections. Resolve ambiguous/orphan rows before applying "
            "revision 20260813_06."
        )
    op.alter_column("inspection_results", "inspection_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    op.drop_constraint("fk_inspection_results_inspection", "inspection_results", type_="foreignkey")
    op.drop_column("inspection_results", "inspection_id")
