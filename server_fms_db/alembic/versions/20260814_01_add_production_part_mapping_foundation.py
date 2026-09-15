"""Add normalized authoritative Part mapping foundations for Cell payloads.

Revision ID: 20260814_01
Revises: 20260813_07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_01"
down_revision = "20260813_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_product_bom_items_required_quantity_positive",
        "product_bom_items",
        "required_quantity >= 1",
    )
    op.create_table(
        "assembly_recipe_stage_parts",
        sa.Column("recipe_stage_part_id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipe_stage_id",
            sa.Integer(),
            sa.ForeignKey("assembly_recipe_stages.recipe_stage_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "part_id",
            sa.Integer(),
            sa.ForeignKey("parts.part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_assembly_recipe_stage_parts_quantity_positive"),
        sa.UniqueConstraint("recipe_stage_id", "part_id", name="uq_assembly_recipe_stage_parts_part"),
    )
    op.create_table(
        "part_cell_locations",
        sa.Column("part_cell_location_id", sa.Integer(), primary_key=True),
        sa.Column(
            "part_id",
            sa.Integer(),
            sa.ForeignKey("parts.part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slot", sa.String(length=100), nullable=False),
        sa.Column("zone", sa.String(length=100), nullable=True),
        sa.CheckConstraint("length(trim(slot)) > 0", name="ck_part_cell_locations_slot_nonblank"),
        sa.UniqueConstraint("part_id", name="uq_part_cell_locations_part"),
        sa.UniqueConstraint("slot", name="uq_part_cell_locations_slot"),
    )
    roof_option = postgresql.ENUM(
        "ROOF_01", "ROOF_02", name="roof_option_code", create_type=False
    )
    op.create_table(
        "product_roof_option_parts",
        sa.Column("product_roof_option_part_id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.product_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("roof_option_code", roof_option, nullable=False),
        sa.Column(
            "part_id",
            sa.Integer(),
            sa.ForeignKey("parts.part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_product_roof_option_parts_quantity_positive"),
        sa.UniqueConstraint(
            "product_id", "roof_option_code", name="uq_product_roof_option_parts_option"
        ),
    )


def downgrade() -> None:
    op.drop_table("product_roof_option_parts")
    op.drop_table("part_cell_locations")
    op.drop_table("assembly_recipe_stage_parts")
    op.drop_constraint(
        "ck_product_bom_items_required_quantity_positive",
        "product_bom_items",
        type_="check",
    )
