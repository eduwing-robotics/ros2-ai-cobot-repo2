"""simplify_material_flow

Revision ID: e8f2dbf47163
Revises: 7c4d9e1a2b3c
Create Date: 2026-08-17 11:01:48.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8f2dbf47163'
down_revision: Union[str, None] = '7c4d9e1a2b3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop job_step_delivery_dependencies
    op.drop_table("job_step_delivery_dependencies")

    # 2. Add job_step_id to job_material_delivery_items and update constraints
    op.add_column("job_material_delivery_items", sa.Column("job_step_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_job_material_delivery_items_job_step_id",
        "job_material_delivery_items",
        "job_steps",
        ["job_step_id"],
        ["job_step_id"],
        ondelete="RESTRICT"
    )
    op.drop_constraint("uq_job_material_delivery_items_part", "job_material_delivery_items", type_="unique")
    op.create_unique_constraint(
        "uq_job_material_delivery_items_step_part",
        "job_material_delivery_items",
        ["job_delivery_id", "job_step_id", "part_id"]
    )

    # 3. Drop source_delivery_batch_id from job_material_deliveries
    op.drop_constraint("job_material_deliveries_source_delivery_batch_id_fkey", "job_material_deliveries", type_="foreignkey")
    op.drop_column("job_material_deliveries", "source_delivery_batch_id")

    # 4. Drop master delivery tables
    op.drop_table("material_delivery_batch_stage_dependencies")
    op.drop_table("material_delivery_batch_items")
    op.drop_table("material_delivery_batches")
    op.drop_table("material_delivery_plans")


def downgrade() -> None:
    # Recreate master delivery tables
    op.create_table(
        "material_delivery_plans",
        sa.Column("delivery_plan_id", sa.Integer(), primary_key=True),
        sa.Column("assembly_recipe_id", sa.Integer(), sa.ForeignKey("assembly_recipes.recipe_id", ondelete="RESTRICT")),
        sa.Column("version", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), server_default="false"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("assembly_recipe_id", "version", name="uq_material_delivery_plans_recipe_version")
    )
    op.create_index(
        "idx_material_delivery_plans_active_per_recipe",
        "material_delivery_plans",
        ["assembly_recipe_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true")
    )
    op.create_table(
        "material_delivery_batches",
        sa.Column("delivery_batch_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_plan_id", sa.Integer(), sa.ForeignKey("material_delivery_plans.delivery_plan_id", ondelete="RESTRICT")),
        sa.Column("batch_order", sa.Integer()),
        sa.Column("delivery_code", sa.String(80)),
        sa.Column("display_name", sa.String(150)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("delivery_plan_id", "batch_order", name="uq_material_delivery_batches_order")
    )
    op.create_table(
        "material_delivery_batch_items",
        sa.Column("batch_item_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT")),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.part_id", ondelete="RESTRICT")),
        sa.Column("quantity", sa.Integer()),
        sa.UniqueConstraint("delivery_batch_id", "part_id", name="uq_material_delivery_batch_items_batch_part")
    )
    op.create_table(
        "material_delivery_batch_stage_dependencies",
        sa.Column("delivery_batch_stage_dependency_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT")),
        sa.Column("recipe_stage_id", sa.Integer(), sa.ForeignKey("assembly_recipe_stages.recipe_stage_id", ondelete="RESTRICT")),
        sa.UniqueConstraint("delivery_batch_id", "recipe_stage_id", name="uq_material_delivery_batch_stage_dependency")
    )

    # Re-add source_delivery_batch_id
    op.add_column("job_material_deliveries", sa.Column("source_delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT"), nullable=True))

    # Revert job_material_delivery_items
    op.drop_constraint("uq_job_material_delivery_items_step_part", "job_material_delivery_items", type_="unique")
    op.create_unique_constraint("uq_job_material_delivery_items_part", "job_material_delivery_items", ["job_delivery_id", "part_id"])
    op.drop_constraint("fk_job_material_delivery_items_job_step_id", "job_material_delivery_items", type_="foreignkey")
    op.drop_column("job_material_delivery_items", "job_step_id")

    # Recreate job_step_delivery_dependencies
    op.create_table(
        "job_step_delivery_dependencies",
        sa.Column("step_dependency_id", sa.Integer(), primary_key=True),
        sa.Column("job_step_id", sa.Integer(), sa.ForeignKey("job_steps.job_step_id", ondelete="RESTRICT")),
        sa.Column("job_delivery_id", sa.Integer(), sa.ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT")),
        sa.Column("is_enforced", sa.Boolean(), server_default="true"),
        sa.UniqueConstraint("job_step_id", "job_delivery_id", name="uq_job_step_delivery_dependency")
    )
