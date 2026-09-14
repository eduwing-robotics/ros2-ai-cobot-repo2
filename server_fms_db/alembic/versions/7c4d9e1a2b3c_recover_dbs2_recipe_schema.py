"""Recover the DB-S2 simplified recipe schema reproducibly.

Revision ID: 7c4d9e1a2b3c
Revises: 59321fa37472
"""

from alembic import op
import sqlalchemy as sa


revision = "7c4d9e1a2b3c"
down_revision = "59321fa37472"
branch_labels = None
depends_on = None


def _add_enum_value(type_name: str, value: str) -> None:
    # Existing dev/test databases received these values manually.  A duplicate
    # check keeps the corrective migration safe there and on a clean chain.
    op.execute(
        "DO $$ BEGIN "
        f"ALTER TYPE {type_name} ADD VALUE '{value}'; "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )


def upgrade() -> None:
    _add_enum_value("production_event_type", "INSPECTION_STARTED")
    _add_enum_value("production_inspection_status", "RUNNING")

    # 59321 recreated these tables after 034f had intentionally removed them.
    op.drop_table("assembly_recipe_stage_part_installation_slots")
    op.drop_table("product_roof_option_part_installation_slots")
    op.drop_table("assembly_recipe_stage_parts")
    op.drop_table("product_roof_option_parts")
    op.drop_table("product_bom_items")

    op.add_column("assembly_recipe_stages", sa.Column("part_id", sa.Integer(), nullable=True))
    op.add_column("assembly_recipe_stages", sa.Column("quantity", sa.Integer(), nullable=True))
    op.add_column("assembly_recipe_stages", sa.Column("installation_slot_id", sa.Integer(), nullable=True))
    op.add_column("assembly_recipe_stages", sa.Column("pick_zone", sa.String(length=100), nullable=True))
    op.add_column("assembly_recipe_stages", sa.Column("option_code", sa.String(length=100), nullable=True))
    op.add_column("assembly_recipe_stages", sa.Column("execution_gate", sa.String(length=100), nullable=True))
    op.create_foreign_key("fk_recipe_stage_part", "assembly_recipe_stages", "parts", ["part_id"], ["part_id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_recipe_stage_slot", "assembly_recipe_stages", "installation_slots", ["installation_slot_id"], ["installation_slot_id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_recipe_stage_quantity_positive", "assembly_recipe_stages", "quantity IS NULL OR quantity >= 1")

    # Immutable executable mapping snapshot.  Existing legacy JobSteps stay
    # nullable and therefore continue to fail closed rather than being invented.
    op.add_column("job_steps", sa.Column("part_id", sa.Integer(), nullable=True))
    op.add_column("job_steps", sa.Column("part_code", sa.String(length=50), nullable=True))
    op.add_column("job_steps", sa.Column("quantity", sa.Integer(), nullable=True))
    op.add_column("job_steps", sa.Column("installation_slot_id", sa.Integer(), nullable=True))
    op.add_column("job_steps", sa.Column("slot_code", sa.String(length=100), nullable=True))
    op.add_column("job_steps", sa.Column("pick_zone", sa.String(length=100), nullable=True))
    op.add_column("job_steps", sa.Column("roof_option_code", sa.String(length=100), nullable=True))
    op.create_foreign_key("fk_job_step_part", "job_steps", "parts", ["part_id"], ["part_id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_job_step_slot", "job_steps", "installation_slots", ["installation_slot_id"], ["installation_slot_id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_job_step_quantity_positive", "job_steps", "quantity IS NULL OR quantity >= 1")

    op.execute("DROP INDEX IF EXISTS uq_job_steps_install_roof")
    op.execute("CREATE UNIQUE INDEX uq_job_steps_install_roof ON job_steps (job_id) WHERE operation_code = 'INSTALL_ROOF'")
    op.execute("DROP INDEX IF EXISTS uq_pending_production_requests_active_session")
    op.execute("CREATE UNIQUE INDEX uq_pending_production_requests_active_session ON pending_production_requests (session_id) WHERE state IN ('WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')")
    op.execute("DROP INDEX IF EXISTS uq_production_jobs_pending_source_item")
    op.execute("CREATE UNIQUE INDEX uq_production_jobs_pending_source_item ON production_jobs (source_pending_request_id, source_item_index) WHERE source_pending_request_id IS NOT NULL AND source_item_index IS NOT NULL")

    op.drop_constraint("uq_installation_slots_product_code", "installation_slots", type_="unique")
    op.create_unique_constraint("uq_installation_slots_slot_code", "installation_slots", ["slot_code"])


def downgrade() -> None:
    pass
