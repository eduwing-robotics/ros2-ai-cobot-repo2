"""Replace Part-owned locations with Product installation targets and pick zones.

Revision ID: 20260814_03
Revises: 20260814_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_03"
down_revision = "20260814_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # M3-A/A2 mapping tables contain no Production rows. Their ownership model is
    # semantically incorrect: Cell slot is an installation target, not a Part's
    # storage/pick location.
    op.drop_table("product_roof_option_part_locations")
    op.drop_table("assembly_recipe_stage_part_locations")
    op.drop_table("part_cell_locations")
    op.create_table(
        "installation_slots",
        sa.Column("installation_slot_id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.product_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slot_code", sa.String(length=100), nullable=False),
        sa.CheckConstraint("length(trim(slot_code)) > 0", name="ck_installation_slots_code_nonblank"),
        sa.UniqueConstraint("slot_code", name="uq_installation_slots_code"),
    )
    op.create_table(
        "assembly_recipe_stage_part_installation_slots",
        sa.Column("stage_part_installation_slot_id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipe_stage_part_id",
            sa.Integer(),
            sa.ForeignKey("assembly_recipe_stage_parts.recipe_stage_part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "installation_slot_id",
            sa.Integer(),
            sa.ForeignKey("installation_slots.installation_slot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("pick_zone", sa.String(length=100), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1", name="ck_stage_part_installation_slots_quantity_positive"
        ),
        sa.UniqueConstraint(
            "recipe_stage_part_id",
            "installation_slot_id",
            name="uq_stage_part_installation_slots_slot",
        ),
    )
    op.create_table(
        "product_roof_option_part_installation_slots",
        sa.Column("roof_part_installation_slot_id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_roof_option_part_id",
            sa.Integer(),
            sa.ForeignKey("product_roof_option_parts.product_roof_option_part_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "installation_slot_id",
            sa.Integer(),
            sa.ForeignKey("installation_slots.installation_slot_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("pick_zone", sa.String(length=100), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity >= 1", name="ck_roof_part_installation_slots_quantity_positive"
        ),
        sa.UniqueConstraint(
            "product_roof_option_part_id",
            "installation_slot_id",
            name="uq_roof_part_installation_slots_slot",
        ),
    )


def downgrade() -> None:
    op.drop_table("product_roof_option_part_installation_slots")
    op.drop_table("assembly_recipe_stage_part_installation_slots")
    op.drop_table("installation_slots")
    op.create_table(
        "part_cell_locations",
        sa.Column("part_cell_location_id", sa.Integer(), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.part_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("slot", sa.String(length=100), nullable=False),
        sa.Column("zone", sa.String(length=100), nullable=True),
        sa.CheckConstraint("length(trim(slot)) > 0", name="ck_part_cell_locations_slot_nonblank"),
        sa.UniqueConstraint("slot", name="uq_part_cell_locations_slot"),
    )
    op.create_table(
        "assembly_recipe_stage_part_locations",
        sa.Column("recipe_stage_part_location_id", sa.Integer(), primary_key=True),
        sa.Column("recipe_stage_part_id", sa.Integer(), sa.ForeignKey("assembly_recipe_stage_parts.recipe_stage_part_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("part_cell_location_id", sa.Integer(), sa.ForeignKey("part_cell_locations.part_cell_location_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_assembly_recipe_stage_part_locations_quantity_positive"),
        sa.UniqueConstraint("recipe_stage_part_id", "part_cell_location_id", name="uq_assembly_recipe_stage_part_locations_location"),
    )
    op.create_table(
        "product_roof_option_part_locations",
        sa.Column("product_roof_option_part_location_id", sa.Integer(), primary_key=True),
        sa.Column("product_roof_option_part_id", sa.Integer(), sa.ForeignKey("product_roof_option_parts.product_roof_option_part_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("part_cell_location_id", sa.Integer(), sa.ForeignKey("part_cell_locations.part_cell_location_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name="ck_product_roof_option_part_locations_quantity_positive"),
        sa.UniqueConstraint("product_roof_option_part_id", "part_cell_location_id", name="uq_product_roof_option_part_locations_location"),
    )
