"""Add Phase 1 material logistics policy snapshots and physical-ready evidence.

Revision ID: 20260901_01
Revises: 476988d67923
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_01"
down_revision = "476988d67923"
branch_labels = None
depends_on = None


def upgrade() -> None:
    supply_mode = postgresql.ENUM("TRANSPORTED", "MANUAL", name="supply_mode")
    supply_mode.create(op.get_bind(), checkfirst=True)
    supply_mode = postgresql.ENUM("TRANSPORTED", "MANUAL", name="supply_mode", create_type=False)

    for table_name in ("assembly_recipe_stages", "job_steps"):
        op.add_column(table_name, sa.Column("supply_mode", supply_mode, nullable=True))
        op.add_column(table_name, sa.Column("supply_group_code", sa.String(length=100), nullable=True))
        op.add_column(table_name, sa.Column("supply_destination_code", sa.String(length=100), nullable=True))

    op.add_column("job_material_deliveries", sa.Column("supply_mode", supply_mode, nullable=True))
    op.add_column("job_material_deliveries", sa.Column("supply_group_code", sa.String(length=100), nullable=True))
    op.add_column("job_material_deliveries", sa.Column("supply_destination_code", sa.String(length=100), nullable=True))
    op.add_column("job_material_deliveries", sa.Column("physical_ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_material_deliveries", sa.Column("physical_ready_request_id", sa.String(length=100), nullable=True))
    op.create_unique_constraint(
        "uq_job_material_deliveries_job_supply_group",
        "job_material_deliveries",
        ["production_job_id", "supply_group_code"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_job_material_deliveries_job_supply_group",
        "job_material_deliveries",
        type_="unique",
    )
    for column_name in (
        "physical_ready_request_id",
        "physical_ready_at",
        "supply_destination_code",
        "supply_group_code",
        "supply_mode",
    ):
        op.drop_column("job_material_deliveries", column_name)

    for table_name in ("job_steps", "assembly_recipe_stages"):
        op.drop_column(table_name, "supply_destination_code")
        op.drop_column(table_name, "supply_group_code")
        op.drop_column(table_name, "supply_mode")

    postgresql.ENUM(name="supply_mode").drop(op.get_bind(), checkfirst=True)
