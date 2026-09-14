"""Create versioned assembly recipe templates and recipe-backed job traces.

Revision ID: 20260813_02
Revises: 20260813_01
"""

from alembic import op
import sqlalchemy as sa

revision = "20260813_02"
down_revision = "20260813_01"
branch_labels = None
depends_on = None

_RECIPE_STAGES = {
    "HOUSE_A": [
        (1, "INSTALL_BASE", "바닥(Base) 설치"),
        (2, "INSTALL_TOILET", "변기 설치"),
        (3, "INSTALL_BASIN", "세면대 설치"),
        (4, "INSTALL_KITCHEN_SINK", "싱크대 설치"),
        (5, "INSTALL_COOKTOP", "가스레인지/조리대 설치"),
        (6, "INSTALL_REFRIGERATOR", "냉장고 설치"),
        (7, "INSTALL_WASHING_MACHINE", "세탁기 설치"),
        (8, "INSTALL_COMMON_INNER_WALL", "공용 내벽 설치"),
        (9, "INSTALL_HOUSE_A_INNER_WALL", "House A 전용 내벽 설치"),
        (10, "INSTALL_LEFT_OUTER_WALL", "좌측 외벽 설치"),
        (11, "INSTALL_DOOR_OUTER_WALL", "출입문용 외벽 설치"),
        (12, "INSTALL_DOOR", "출입문 설치"),
        (13, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치"),
        (14, "INSTALL_WINDOW_01", "창문 1 설치"),
        (15, "INSTALL_REAR_OUTER_WALL", "후면 외벽 설치"),
        (16, "INSTALL_WINDOW_02", "창문 2 설치"),
    ],
    "HOUSE_B": [
        (1, "INSTALL_BASE", "바닥(Base) 설치"),
        (2, "INSTALL_TOILET", "변기 설치"),
        (3, "INSTALL_BASIN", "세면대 설치"),
        (4, "INSTALL_BATHTUB", "욕조 설치"),
        (5, "INSTALL_WASHING_MACHINE", "세탁기 설치"),
        (6, "INSTALL_COOKTOP", "가스레인지/조리대 설치"),
        (7, "INSTALL_KITCHEN_SINK", "싱크대 설치"),
        (8, "INSTALL_REFRIGERATOR", "냉장고 설치"),
        (9, "INSTALL_COMMON_INNER_WALL", "공용 내벽 설치"),
        (10, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치"),
        (11, "INSTALL_REAR_OUTER_WALL", "후면 외벽 설치"),
        (12, "INSTALL_WINDOW_01", "창문 1 설치"),
        (13, "INSTALL_LEFT_OUTER_WALL", "좌측 외벽 설치"),
        (14, "INSTALL_WINDOW_02", "창문 2 설치"),
        (15, "INSTALL_DOOR_OUTER_WALL", "출입문용 외벽 설치"),
        (16, "INSTALL_DOOR", "출입문 설치"),
    ],
}


def upgrade() -> None:
    # Application enums/models are deliberately not imported by this revision.
    op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'PRE_ROOF_READY'")
    op.create_table(
        "assembly_recipes",
        sa.Column("recipe_id", sa.Integer(), primary_key=True),
        sa.Column("product_code", sa.String(length=50), sa.ForeignKey("products.product_code", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("product_code", "version", name="uq_assembly_recipes_product_version"),
    )
    op.create_index("uq_assembly_recipes_active_product", "assembly_recipes", ["product_code"], unique=True, postgresql_where=sa.text("is_active"))
    op.create_table(
        "assembly_recipe_stages",
        sa.Column("recipe_stage_id", sa.Integer(), primary_key=True),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("assembly_recipes.recipe_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("operation_code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=False),
        sa.CheckConstraint("stage_order >= 1", name="ck_assembly_recipe_stages_order_positive"),
        sa.UniqueConstraint("recipe_id", "stage_order", name="uq_assembly_recipe_stages_order"),
    )
    op.add_column("production_jobs", sa.Column("assembly_recipe_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_production_jobs_assembly_recipe", "production_jobs", "assembly_recipes", ["assembly_recipe_id"], ["recipe_id"], ondelete="RESTRICT")
    op.add_column("job_steps", sa.Column("source_recipe_stage_id", sa.Integer(), nullable=True))
    op.add_column("job_steps", sa.Column("step_order", sa.Integer(), nullable=True))
    op.add_column("job_steps", sa.Column("operation_code", sa.String(length=100), nullable=True))
    op.add_column("job_steps", sa.Column("display_name", sa.String(length=150), nullable=True))
    op.alter_column("job_steps", "process_step_id", existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key("fk_job_steps_source_recipe_stage", "job_steps", "assembly_recipe_stages", ["source_recipe_stage_id"], ["recipe_stage_id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_job_step_order", "job_steps", ["job_id", "step_order"])
    op.create_check_constraint("ck_job_steps_recipe_snapshot", "job_steps", "source_recipe_stage_id IS NULL OR (step_order IS NOT NULL AND operation_code IS NOT NULL AND display_name IS NOT NULL)")

    bind = op.get_bind()
    for product_code, stages in _RECIPE_STAGES.items():
        recipe_id = bind.execute(
            sa.text("INSERT INTO assembly_recipes (product_code, version, is_active, description) VALUES (:product_code, 1, true, :description) RETURNING recipe_id"),
            {"product_code": product_code, "description": "PRE-ROOF assembly recipe v1"},
        ).scalar_one()
        bind.execute(
            sa.text("INSERT INTO assembly_recipe_stages (recipe_id, stage_order, operation_code, display_name) VALUES (:recipe_id, :stage_order, :operation_code, :display_name)"),
            [{"recipe_id": recipe_id, "stage_order": order, "operation_code": operation, "display_name": name} for order, operation, name in stages],
        )


def downgrade() -> None:
    op.drop_constraint("ck_job_steps_recipe_snapshot", "job_steps", type_="check")
    op.drop_constraint("uq_job_step_order", "job_steps", type_="unique")
    op.drop_constraint("fk_job_steps_source_recipe_stage", "job_steps", type_="foreignkey")
    op.alter_column("job_steps", "process_step_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("job_steps", "display_name")
    op.drop_column("job_steps", "operation_code")
    op.drop_column("job_steps", "step_order")
    op.drop_column("job_steps", "source_recipe_stage_id")
    op.drop_constraint("fk_production_jobs_assembly_recipe", "production_jobs", type_="foreignkey")
    op.drop_column("production_jobs", "assembly_recipe_id")
    op.drop_table("assembly_recipe_stages")
    op.drop_index("uq_assembly_recipes_active_product", table_name="assembly_recipes")
    op.drop_table("assembly_recipes")
    # PostgreSQL enum values cannot be removed safely while historical rows may use them.
