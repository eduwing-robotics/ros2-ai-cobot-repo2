"""Add durable inventory reservation and execution-consumption fields.

Revision ID: 20260907_02
Revises: 20260907_01
Create Date: 2026-09-07
"""

from alembic import op
import sqlalchemy as sa

revision = "20260907_02"
down_revision = "20260907_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("inventory") as batch_op:
        batch_op.add_column(
            sa.Column("reserved_quantity", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_inventory_quantity_nonnegative", "quantity >= 0"
        )
        batch_op.create_check_constraint(
            "ck_inventory_reserved_quantity_nonnegative", "reserved_quantity >= 0"
        )
        batch_op.create_check_constraint(
            "ck_inventory_reserved_not_above_quantity", "reserved_quantity <= quantity"
        )
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("inventory_reservation_released_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("job_steps") as batch_op:
        batch_op.add_column(
            sa.Column("inventory_consumed_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("inventory_movements") as batch_op:
        batch_op.add_column(
            sa.Column("job_step_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_inventory_movements_job_step_id_job_steps",
            "job_steps", ["job_step_id"], ["job_step_id"], ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_inventory_movements_step_part_type",
            ["job_step_id", "part_code", "movement_type"],
        )


def downgrade() -> None:
    with op.batch_alter_table("inventory_movements") as batch_op:
        batch_op.drop_constraint("uq_inventory_movements_step_part_type", type_="unique")
        batch_op.drop_constraint("fk_inventory_movements_job_step_id_job_steps", type_="foreignkey")
        batch_op.drop_column("job_step_id")
    with op.batch_alter_table("job_steps") as batch_op:
        batch_op.drop_column("inventory_consumed_at")
    with op.batch_alter_table("production_jobs") as batch_op:
        batch_op.drop_column("inventory_reservation_released_at")
    with op.batch_alter_table("inventory") as batch_op:
        batch_op.drop_constraint("ck_inventory_reserved_not_above_quantity", type_="check")
        batch_op.drop_constraint("ck_inventory_reserved_quantity_nonnegative", type_="check")
        batch_op.drop_constraint("ck_inventory_quantity_nonnegative", type_="check")
        batch_op.drop_column("reserved_quantity")
