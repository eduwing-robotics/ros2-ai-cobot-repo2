"""Persist incomplete production-create drafts for multi-turn Voice flow.

Revision ID: 20260908_01
Revises: 20260907_02
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa

revision = "20260908_01"
down_revision = "20260907_02"
branch_labels = None
depends_on = None

_ACTIVE = "state IN ('COLLECTING_DETAILS', 'WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')"
_OLD_ACTIVE = "state IN ('WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # PostgreSQL permits ADD VALUE in a transaction, but the new value
        # cannot be used by this revision's partial-index predicate until it
        # has committed. This deliberate boundary is retry-safe because of
        # IF NOT EXISTS if a later schema operation fails.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE pending_production_state ADD VALUE IF NOT EXISTS 'COLLECTING_DETAILS'")
    op.drop_index("uq_pending_production_requests_active_session", table_name="pending_production_requests")
    with op.batch_alter_table("pending_production_requests") as batch_op:
        batch_op.alter_column("product_code", existing_type=sa.String(length=100), nullable=True)
    op.create_index(
        "uq_pending_production_requests_active_session", "pending_production_requests", ["session_id"],
        unique=True, postgresql_where=sa.text(_ACTIVE), sqlite_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed in-place. Downgrade remains safe
    # only after no incomplete drafts exist; operators must resolve/expire them.
    bind = op.get_bind()
    # product_code NULL is the authoritative incompatible shape. A draft may
    # already be EXPIRED or REJECTED, so state alone cannot make the NOT NULL
    # restoration safe. Never delete, invent, or backfill a Product here.
    has_null_product_code = bind.execute(
        sa.text("SELECT 1 FROM pending_production_requests WHERE product_code IS NULL LIMIT 1")
    ).scalar() is not None
    if has_null_product_code:
        raise RuntimeError(
            "Cannot downgrade pending production draft schema while "
            "pending_production_requests.product_code IS NULL rows exist; "
            "cleanup or canonical Product backfill is required first."
        )
    if bind.dialect.name == "postgresql":
        op.execute("""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pending_production_requests WHERE state = 'COLLECTING_DETAILS') THEN
                RAISE EXCEPTION 'Cannot downgrade while COLLECTING_DETAILS requests exist';
              END IF;
            END $$;
        """)
    op.drop_index("uq_pending_production_requests_active_session", table_name="pending_production_requests")
    with op.batch_alter_table("pending_production_requests") as batch_op:
        batch_op.alter_column("product_code", existing_type=sa.String(length=100), nullable=False)
    op.create_index(
        "uq_pending_production_requests_active_session", "pending_production_requests", ["session_id"],
        unique=True, postgresql_where=sa.text(_OLD_ACTIVE), sqlite_where=sa.text(_OLD_ACTIVE),
    )
