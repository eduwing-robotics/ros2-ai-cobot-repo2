"""Add durable Incoming QA v0.2 transaction foundation.

Revision ID: 20260904_01
Revises: 20260903_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260904_01"
down_revision = "20260903_03"
branch_labels = None
depends_on = None

LEGACY_REQUEST_UNIQUE = "material_inspections_inspection_request_id_key"
TRANSACTION_ITEM_UNIQUE = "uq_material_inspections_transaction_item"
TRANSACTION_ITEM_FK = "fk_material_inspections_incoming_qa_transaction"


def upgrade() -> None:
    transaction_status = sa.Enum(
        "REQUESTED", "SENT", "ACKED", "COMPLETED", "REJECTED",
        name="incoming_qa_transaction_status",
    )
    material_result = postgresql.ENUM(
        "PASS", "FAIL", "NOT_EVALUATED",
        name="material_inspection_result",
        create_type=False,
    )
    op.create_table(
        "incoming_qa_transactions",
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("inspection_request_id", sa.String(length=100), nullable=False),
        sa.Column("production_job_id", sa.Integer(), nullable=False),
        sa.Column("inspection_mode", sa.String(length=50), nullable=False),
        sa.Column("inspection_cycle", sa.Integer(), nullable=False),
        sa.Column("status", transaction_status, nullable=False),
        sa.Column("ack_accepted", sa.Boolean(), nullable=True),
        sa.Column("ack_duplicate", sa.Boolean(), nullable=True),
        sa.Column("ack_reason_code", sa.String(length=100), nullable=True),
        sa.Column("overall_result", material_result, nullable=True),
        sa.Column("production_valid", sa.Boolean(), nullable=True),
        sa.Column("immutable_request_snapshot", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("inspection_cycle >= 1", name="ck_incoming_qa_transactions_cycle"),
        sa.CheckConstraint("retry_count >= 0", name="ck_incoming_qa_transactions_retry_count"),
        sa.ForeignKeyConstraint(["production_job_id"], ["production_jobs.job_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint("inspection_request_id", name="uq_incoming_qa_transactions_request_id"),
    )
    op.add_column(
        "material_inspections",
        sa.Column("incoming_qa_transaction_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "material_inspections",
        sa.Column("predicted_class_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "material_inspections",
        sa.Column("material_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "material_inspections",
        sa.Column("result_detail_json", sa.Text(), nullable=True),
    )
    # Legacy v0.1 rows remain relation-null and retain their unique request IDs.
    # v0.2 moves request identity authority to incoming_qa_transactions.
    op.drop_constraint(LEGACY_REQUEST_UNIQUE, "material_inspections", type_="unique")
    op.create_foreign_key(
        TRANSACTION_ITEM_FK,
        "material_inspections",
        "incoming_qa_transactions",
        ["incoming_qa_transaction_id"],
        ["transaction_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        TRANSACTION_ITEM_UNIQUE,
        "material_inspections",
        ["incoming_qa_transaction_id", "delivery_item_id"],
    )
    op.create_check_constraint(
        "ck_material_inspections_material_confidence",
        "material_inspections",
        "material_confidence IS NULL OR (material_confidence >= 0 AND material_confidence <= 1)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    transaction_rows = bind.execute(sa.text("SELECT count(*) FROM incoming_qa_transactions")).scalar_one()
    if transaction_rows:
        raise RuntimeError(
            "Cannot downgrade Incoming QA v0.2 foundation while incoming_qa_transactions contain data."
        )
    v02_evidence = bind.execute(sa.text("""
        SELECT count(*)
        FROM material_inspections
        WHERE incoming_qa_transaction_id IS NOT NULL
           OR predicted_class_name IS NOT NULL
           OR material_confidence IS NOT NULL
           OR result_detail_json IS NOT NULL
    """)).scalar_one()
    if v02_evidence:
        raise RuntimeError(
            "Cannot downgrade Incoming QA v0.2 foundation while v0.2 inspection evidence exists."
        )
    duplicate_request_ids = bind.execute(sa.text("""
        SELECT inspection_request_id
        FROM material_inspections
        GROUP BY inspection_request_id
        HAVING count(*) > 1
        LIMIT 1
    """)).first()
    if duplicate_request_ids is not None:
        raise RuntimeError(
            "Cannot restore legacy MaterialInspection request-ID uniqueness while duplicate request IDs exist."
        )

    op.drop_constraint("ck_material_inspections_material_confidence", "material_inspections", type_="check")
    op.drop_constraint(TRANSACTION_ITEM_UNIQUE, "material_inspections", type_="unique")
    op.drop_constraint(TRANSACTION_ITEM_FK, "material_inspections", type_="foreignkey")
    op.drop_column("material_inspections", "result_detail_json")
    op.drop_column("material_inspections", "material_confidence")
    op.drop_column("material_inspections", "predicted_class_name")
    op.drop_column("material_inspections", "incoming_qa_transaction_id")
    op.create_unique_constraint(
        LEGACY_REQUEST_UNIQUE,
        "material_inspections",
        ["inspection_request_id"],
    )
    op.drop_table("incoming_qa_transactions")
    sa.Enum(name="incoming_qa_transaction_status").drop(bind, checkfirst=True)
