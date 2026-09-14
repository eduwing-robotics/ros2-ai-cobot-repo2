"""Generalize terminal and gated-stage lifecycle metadata.

Revision ID: 20260903_03
Revises: 20260903_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_03"
down_revision = "20260903_02"
branch_labels = None
depends_on = None


TERMINAL_DEFAULT = sa.text("false")
ITEM_STAGE_FK = "fk_job_material_delivery_items_source_recipe_stage"
JOB_STAGE_UNIQUE = "uq_job_steps_job_source_recipe_stage"
LEGACY_ROOF_INDEX = "uq_job_steps_install_roof"


def upgrade() -> None:
    op.add_column("assembly_recipe_stages", sa.Column("is_terminal", sa.Boolean(), nullable=True, server_default=TERMINAL_DEFAULT))
    op.add_column("job_steps", sa.Column("is_terminal", sa.Boolean(), nullable=True, server_default=TERMINAL_DEFAULT))
    op.add_column("job_material_delivery_items", sa.Column("source_recipe_stage_id", sa.Integer(), nullable=True))

    # This one-time backfill is intentionally migration-only; runtime code uses metadata.
    op.execute("""
        UPDATE assembly_recipe_stages AS stage
        SET is_terminal = true
        FROM assembly_recipes AS recipe
        WHERE recipe.recipe_id = stage.recipe_id
          AND recipe.product_code = $$HOUSE_B$$
          AND recipe.is_active
          AND stage.operation_code = $$INSTALL_ROOF$$
    """)
    op.execute("""
        UPDATE job_steps AS step
        SET is_terminal = stage.is_terminal
        FROM assembly_recipe_stages AS stage
        WHERE step.source_recipe_stage_id = stage.recipe_stage_id
    """)
    op.execute("""
        UPDATE job_material_delivery_items AS item
        SET source_recipe_stage_id = step.source_recipe_stage_id
        FROM job_steps AS step
        WHERE item.job_step_id = step.job_step_id
          AND step.source_recipe_stage_id IS NOT NULL
    """)
    op.execute("""
        UPDATE job_material_delivery_items AS item
        SET source_recipe_stage_id = stage.recipe_stage_id
        FROM job_material_deliveries AS delivery
        JOIN production_jobs AS job ON job.job_id = delivery.production_job_id
        JOIN assembly_recipe_stages AS stage ON stage.recipe_id = job.assembly_recipe_id
        WHERE item.job_delivery_id = delivery.job_delivery_id
          AND item.job_step_id IS NULL
          AND stage.execution_gate = $$PRE_ROOF_PASS$$
          AND stage.part_code = item.part_code
          AND stage.quantity = item.quantity
          AND stage.supply_group_code = delivery.supply_group_code
          AND (stage.option_code IS NULL OR stage.option_code = CAST(job.roof_option_code AS text))
    """)

    op.alter_column("assembly_recipe_stages", "is_terminal", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("job_steps", "is_terminal", existing_type=sa.Boolean(), nullable=False)
    op.create_foreign_key(ITEM_STAGE_FK, "job_material_delivery_items", "assembly_recipe_stages", ["source_recipe_stage_id"], ["recipe_stage_id"], ondelete="RESTRICT")
    op.drop_index(LEGACY_ROOF_INDEX, table_name="job_steps")
    op.create_unique_constraint(JOB_STAGE_UNIQUE, "job_steps", ["job_id", "source_recipe_stage_id"])


def downgrade() -> None:
    op.drop_constraint(JOB_STAGE_UNIQUE, "job_steps", type_="unique")
    op.create_index(
        LEGACY_ROOF_INDEX,
        "job_steps",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("operation_code = $$INSTALL_ROOF$$"),
        sqlite_where=sa.text("operation_code = $$INSTALL_ROOF$$"),
    )
    op.drop_constraint(ITEM_STAGE_FK, "job_material_delivery_items", type_="foreignkey")
    op.drop_column("job_material_delivery_items", "source_recipe_stage_id")
    op.drop_column("job_steps", "is_terminal")
    op.drop_column("assembly_recipe_stages", "is_terminal")
