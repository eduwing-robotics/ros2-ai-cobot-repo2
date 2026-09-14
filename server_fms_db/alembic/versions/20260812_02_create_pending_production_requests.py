"""Create persistent pending production request state.

Revision ID: 20260812_02
Revises: 20260812_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260812_02"
down_revision = "20260812_01"
branch_labels = None
depends_on = None

_PENDING_PRODUCTION_STATE = postgresql.ENUM(
    "WAITING_ROOF_OPTION",
    "AWAITING_CONFIRMATION",
    "CONFIRMED",
    "REJECTED",
    "EXPIRED",
    name="pending_production_state",
)


def upgrade() -> None:
    _PENDING_PRODUCTION_STATE.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "pending_production_requests",
        sa.Column("request_id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(length=100), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                "WAITING_ROOF_OPTION",
                "AWAITING_CONFIRMATION",
                "CONFIRMED",
                "REJECTED",
                "EXPIRED",
                name="pending_production_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "product_code",
            sa.String(length=50),
            sa.ForeignKey("products.product_code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "roof_option_code",
            postgresql.ENUM(
                "ROOF_01",
                "ROOF_02",
                name="roof_option_code",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_pending_production_requests_active_session",
        "pending_production_requests",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')"),
    )
    op.create_index(
        "ix_pending_production_requests_expires_at",
        "pending_production_requests",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pending_production_requests_expires_at", table_name="pending_production_requests")
    op.drop_index("uq_pending_production_requests_active_session", table_name="pending_production_requests")
    op.drop_table("pending_production_requests")
    _PENDING_PRODUCTION_STATE.drop(op.get_bind(), checkfirst=True)
