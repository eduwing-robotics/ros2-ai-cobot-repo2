"""Allow shared Parts to have stage-specific semantic Cell locations.

Revision ID: 20260814_02
Revises: 20260814_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_02"
down_revision = "20260814_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A Part may legitimately appear at several Cell semantic slots.  Slot itself
    # remains globally unique because it is the Cell completion/rework key.
    op.drop_constraint("uq_part_cell_locations_part", "part_cell_locations", type_="unique")
    op.create_table(
        "assembly_recipe_stage_part_locations",
        sa.Column("recipe_stage_part_location_id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipe_stage_part_id",
            sa.Integer(),
            sa.ForeignKey("assembly_recipe_stage_parts.recipe_stage_part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "part_cell_location_id",
            sa.Integer(),
            sa.ForeignKey("part_cell_locations.part_cell_location_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1", name="ck_assembly_recipe_stage_part_locations_quantity_positive"
        ),
        sa.UniqueConstraint(
            "recipe_stage_part_id",
            "part_cell_location_id",
            name="uq_assembly_recipe_stage_part_locations_location",
        ),
    )
    op.create_table(
        "product_roof_option_part_locations",
        sa.Column("product_roof_option_part_location_id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_roof_option_part_id",
            sa.Integer(),
            sa.ForeignKey("product_roof_option_parts.product_roof_option_part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "part_cell_location_id",
            sa.Integer(),
            sa.ForeignKey("part_cell_locations.part_cell_location_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1", name="ck_product_roof_option_part_locations_quantity_positive"
        ),
        sa.UniqueConstraint(
            "product_roof_option_part_id",
            "part_cell_location_id",
            name="uq_product_roof_option_part_locations_location",
        ),
    )


def downgrade() -> None:
    op.drop_table("product_roof_option_part_locations")
    op.drop_table("assembly_recipe_stage_part_locations")
    op.create_unique_constraint(
        "uq_part_cell_locations_part", "part_cell_locations", ["part_id"]
    )
