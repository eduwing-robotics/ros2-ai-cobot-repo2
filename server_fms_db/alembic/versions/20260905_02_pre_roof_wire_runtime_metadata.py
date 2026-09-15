"""Persisted PRE_ROOF v0.1 wire transport/idempotency metadata.

Revision ID: 20260905_02
Revises: 20260905_01

This file is source-only in this change; it is intentionally not applied to
production or benchmark databases by tests or tooling.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260905_02"
down_revision = "20260905_01"
branch_labels = None
depends_on = None


WIRE_COLUMNS = (
    ("wire_request_snapshot_json", sa.Text(), True),
    ("wire_request_digest", sa.String(length=64), True),
    ("wire_result_digest", sa.String(length=64), True),
    ("wire_error_code", sa.String(length=100), True),
    ("wire_sent_at", sa.DateTime(timezone=True), True),
    ("wire_acked_at", sa.DateTime(timezone=True), True),
    ("wire_retry_count", sa.Integer(), False),
    ("runtime_profile", sa.String(length=100), True),
    ("runtime_versions_json", sa.Text(), True),
)


def upgrade() -> None:
    for name, type_, nullable in WIRE_COLUMNS:
        if name == "wire_retry_count":
            op.add_column(
                "production_inspections",
                sa.Column(name, type_, nullable=False, server_default=sa.text("0")),
            )
        else:
            op.add_column("production_inspections", sa.Column(name, type_, nullable=nullable))


def downgrade() -> None:
    for name, _, _ in reversed(WIRE_COLUMNS):
        op.drop_column("production_inspections", name)
