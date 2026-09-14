"""Create versioned material delivery plan templates and job snapshots.

Revision ID: 20260813_03
Revises: 20260813_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813_03"
down_revision = "20260813_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This revision deliberately contains no application model or enum imports.
    delivery_status = sa.Enum(
        "PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", name="job_material_delivery_status"
    )
    op.create_table(
        "material_delivery_plans",
        sa.Column("delivery_plan_id", sa.Integer(), primary_key=True),
        sa.Column("assembly_recipe_id", sa.Integer(), sa.ForeignKey("assembly_recipes.recipe_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("assembly_recipe_id", "version", name="uq_material_delivery_plans_recipe_version"),
    )
    op.create_index("uq_material_delivery_plans_active_recipe", "material_delivery_plans", ["assembly_recipe_id"], unique=True, postgresql_where=sa.text("is_active"))
    op.create_table(
        "material_delivery_batches",
        sa.Column("delivery_batch_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_plan_id", sa.Integer(), sa.ForeignKey("material_delivery_plans.delivery_plan_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_order", sa.Integer(), nullable=False),
        sa.Column("delivery_code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=False),
        sa.CheckConstraint("batch_order >= 1", name="ck_material_delivery_batches_order_positive"),
        sa.UniqueConstraint("delivery_plan_id", "batch_order", name="uq_material_delivery_batches_order"),
    )
    op.create_table(
        "material_delivery_batch_items",
        sa.Column("delivery_batch_item_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.part_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_material_delivery_batch_items_quantity_positive"),
        sa.UniqueConstraint("delivery_batch_id", "part_id", name="uq_material_delivery_batch_items_part"),
    )
    op.create_table(
        "material_delivery_batch_stage_dependencies",
        sa.Column("delivery_batch_stage_dependency_id", sa.Integer(), primary_key=True),
        sa.Column("delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("recipe_stage_id", sa.Integer(), sa.ForeignKey("assembly_recipe_stages.recipe_stage_id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("delivery_batch_id", "recipe_stage_id", name="uq_material_delivery_batch_stage_dependency"),
    )
    op.add_column("production_jobs", sa.Column("material_delivery_plan_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_production_jobs_material_delivery_plan", "production_jobs", "material_delivery_plans", ["material_delivery_plan_id"], ["delivery_plan_id"], ondelete="RESTRICT")
    op.create_table(
        "job_material_deliveries",
        sa.Column("job_delivery_id", sa.Integer(), primary_key=True),
        sa.Column("production_job_id", sa.Integer(), sa.ForeignKey("production_jobs.job_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_delivery_batch_id", sa.Integer(), sa.ForeignKey("material_delivery_batches.delivery_batch_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("batch_order", sa.Integer(), nullable=False),
        sa.Column("delivery_code", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=False),
        sa.Column("status", delivery_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("production_job_id", "batch_order", name="uq_job_material_deliveries_order"),
    )
    op.create_table(
        "job_material_delivery_items",
        sa.Column("job_delivery_item_id", sa.Integer(), primary_key=True),
        sa.Column("job_delivery_id", sa.Integer(), sa.ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_delivery_batch_item_id", sa.Integer(), sa.ForeignKey("material_delivery_batch_items.delivery_batch_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.part_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_job_material_delivery_items_quantity_positive"),
        sa.UniqueConstraint("job_delivery_id", "part_id", name="uq_job_material_delivery_items_part"),
    )
    op.create_table(
        "job_step_delivery_dependencies",
        sa.Column("job_step_delivery_dependency_id", sa.Integer(), primary_key=True),
        sa.Column("job_step_id", sa.Integer(), sa.ForeignKey("job_steps.job_step_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_delivery_id", sa.Integer(), sa.ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("job_step_id", "job_delivery_id", name="uq_job_step_delivery_dependency"),
    )


def downgrade() -> None:
    op.drop_table("job_step_delivery_dependencies")
    op.drop_table("job_material_delivery_items")
    op.drop_table("job_material_deliveries")
    op.drop_constraint("fk_production_jobs_material_delivery_plan", "production_jobs", type_="foreignkey")
    op.drop_column("production_jobs", "material_delivery_plan_id")
    op.drop_table("material_delivery_batch_stage_dependencies")
    op.drop_table("material_delivery_batch_items")
    op.drop_table("material_delivery_batches")
    op.drop_index("uq_material_delivery_plans_active_recipe", table_name="material_delivery_plans")
    op.drop_table("material_delivery_plans")
    sa.Enum(name="job_material_delivery_status").drop(op.get_bind(), checkfirst=True)
