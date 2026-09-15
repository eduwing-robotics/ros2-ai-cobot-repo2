"""Canonical PRE_ROOF inspection lifecycle/result foundation.

Revision ID: 20260905_01
Revises: 20260904_02

This revision is intentionally data-preserving and does not grant historical
PASS rows production authority: every migrated production_valid value is false.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260905_01"
down_revision = "20260904_02"
branch_labels = None
depends_on = None

REQUEST_UNIQUE = "uq_production_inspections_request_id"
INSPECTION_CHECK = "ck_production_inspections_status_result_valid"
LEGACY_PASS_CHECK = "ck_production_inspections_legacy_is_passed"
VIEW_RESULT_CHECK = "ck_production_inspection_results_result_legacy"
RESULT_ENUM = "production_inspection_result"


def _add_status_values() -> None:
    # PostgreSQL enum values are global metadata and require autocommit before
    # UPDATE statements can use them. Existing PASSED/FAILED values are retained
    # by PostgreSQL; the row mapping below removes their runtime use.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE production_inspection_status ADD VALUE IF NOT EXISTS 'COMPLETED'")
        op.execute("ALTER TYPE production_inspection_status ADD VALUE IF NOT EXISTS 'ERROR'")


def _backfill_request_ids() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # No uuid-ossp/pgcrypto extension dependency: application-created rows
        # use uuid4; historical rows get unique UUID-shaped, immutable values.
        op.execute("""
            UPDATE production_inspections
            SET inspection_request_id = lower(
                substr(md5('pre-roof:' || inspection_id::text), 1, 8) || '-' ||
                substr(md5('pre-roof:' || inspection_id::text), 9, 4) || '-4' ||
                substr(md5('pre-roof:' || inspection_id::text), 14, 3) || '-8' ||
                substr(md5('pre-roof:' || inspection_id::text), 18, 3) || '-' ||
                substr(md5('pre-roof:' || inspection_id::text), 21, 12)
            )
            WHERE inspection_request_id IS NULL
        """)
    elif bind.dialect.name == "sqlite":
        op.execute("""
            UPDATE production_inspections
            SET inspection_request_id = '00000000-0000-4000-8000-' || printf('%012d', inspection_id)
            WHERE inspection_request_id IS NULL
        """)
    else:
        # All supported production deployments are PostgreSQL. Keep a stable,
        # per-row fallback for other development dialects rather than deriving
        # identity from mutable state.
        op.execute("""
            UPDATE production_inspections
            SET inspection_request_id = 'legacy-pre-roof-' || inspection_id
            WHERE inspection_request_id IS NULL
        """)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _add_status_values()
        result_enum = postgresql.ENUM("PASS", "FAIL", "NOT_EVALUATED", name=RESULT_ENUM)
        result_enum.create(bind, checkfirst=True)
    else:
        result_enum = sa.Enum("PASS", "FAIL", "NOT_EVALUATED", name=RESULT_ENUM)

    op.add_column("production_inspections", sa.Column("inspection_request_id", sa.String(length=36), nullable=True))
    op.add_column("production_inspections", sa.Column("result", result_enum, nullable=True))
    op.add_column("production_inspections", sa.Column("vision_production_valid", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("production_inspections", sa.Column("production_valid", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # Map all historic outcome-bearing statuses before adding canonical checks.
    # IN_PROGRESS appears only in the oldest deployment lineage; current source
    # already uses RUNNING, but preserving it avoids an unsafe migration gap.
    op.execute("""
        UPDATE production_inspections
        SET status = CASE
                WHEN status::text IN ('PASSED', 'FAILED') THEN 'COMPLETED'::production_inspection_status
                WHEN status::text = 'IN_PROGRESS' THEN 'RUNNING'::production_inspection_status
                ELSE status
            END,
            result = CASE
                WHEN status::text = 'PASSED' OR is_passed = true THEN 'PASS'::production_inspection_result
                WHEN status::text = 'FAILED' OR is_passed = false THEN 'FAIL'::production_inspection_result
                ELSE NULL
            END,
            production_valid = false,
            vision_production_valid = false,
            is_passed = CASE
                WHEN status::text = 'PASSED' OR is_passed = true THEN true
                WHEN status::text = 'FAILED' OR is_passed = false THEN false
                ELSE NULL
            END
    """)
    _backfill_request_ids()
    op.alter_column("production_inspections", "inspection_request_id", existing_type=sa.String(length=36), nullable=False)
    op.create_unique_constraint(REQUEST_UNIQUE, "production_inspections", ["inspection_request_id"])
    op.create_check_constraint(
        INSPECTION_CHECK,
        "production_inspections",
        "(status IN ('PENDING', 'RUNNING', 'ERROR') AND result IS NULL AND production_valid = false) "
        "OR (status = 'COMPLETED' AND result IN ('PASS', 'FAIL', 'NOT_EVALUATED') "
        "AND (result = 'PASS' OR production_valid = false))",
    )
    op.create_check_constraint(
        LEGACY_PASS_CHECK,
        "production_inspections",
        "(status IN ('PENDING', 'RUNNING', 'ERROR') AND is_passed IS NULL) "
        "OR (status = 'COMPLETED' AND result = 'PASS' AND is_passed = true) "
        "OR (status = 'COMPLETED' AND result = 'FAIL' AND is_passed = false) "
        "OR (status = 'COMPLETED' AND result = 'NOT_EVALUATED' AND is_passed IS NULL)",
    )

    op.add_column("production_inspection_results", sa.Column("result", result_enum, nullable=True))
    op.execute("""
        UPDATE production_inspection_results
        SET result = CASE WHEN is_passed = true THEN 'PASS'::production_inspection_result ELSE 'FAIL'::production_inspection_result END
        WHERE result IS NULL
    """)
    op.alter_column("production_inspection_results", "result", existing_type=result_enum, nullable=False)
    op.alter_column("production_inspection_results", "is_passed", existing_type=sa.Boolean(), nullable=True)
    op.create_check_constraint(
        VIEW_RESULT_CHECK,
        "production_inspection_results",
        "(result = 'PASS' AND is_passed = true) "
        "OR (result = 'FAIL' AND is_passed = false) "
        "OR (result = 'NOT_EVALUATED' AND is_passed IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    unsupported = bind.execute(sa.text("""
        SELECT count(*) FROM production_inspections
        WHERE status = 'ERROR'
           OR result = 'NOT_EVALUATED'
           OR production_valid = true
           OR vision_production_valid = true
    """)).scalar_one()
    if unsupported:
        raise RuntimeError("Cannot downgrade PRE_ROOF foundation while canonical ERROR/NOT_EVALUATED/validity evidence exists.")
    view_unsupported = bind.execute(sa.text("""
        SELECT count(*) FROM production_inspection_results
        WHERE result = 'NOT_EVALUATED' OR is_passed IS NULL
    """)).scalar_one()
    if view_unsupported:
        raise RuntimeError("Cannot downgrade PRE_ROOF foundation while NOT_EVALUATED view evidence exists.")

    # The old PostgreSQL enum retains PASSED/FAILED values, so only safe PASS/
    # FAIL rows can be translated back. Request identities are intentionally
    # discarded only after the guards prove no new-only semantics would be lost.
    if bind.dialect.name == "postgresql":
        op.execute("""
            UPDATE production_inspections
            SET status = CASE WHEN result = 'PASS' THEN 'PASSED'::production_inspection_status ELSE 'FAILED'::production_inspection_status END
            WHERE status = 'COMPLETED'
        """)
    op.drop_constraint(VIEW_RESULT_CHECK, "production_inspection_results", type_="check")
    op.alter_column("production_inspection_results", "is_passed", existing_type=sa.Boolean(), nullable=False)
    op.drop_column("production_inspection_results", "result")
    op.drop_constraint(LEGACY_PASS_CHECK, "production_inspections", type_="check")
    op.drop_constraint(INSPECTION_CHECK, "production_inspections", type_="check")
    op.drop_constraint(REQUEST_UNIQUE, "production_inspections", type_="unique")
    op.drop_column("production_inspections", "production_valid")
    op.drop_column("production_inspections", "vision_production_valid")
    op.drop_column("production_inspections", "result")
    op.drop_column("production_inspections", "inspection_request_id")
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name=RESULT_ENUM).drop(bind, checkfirst=True)
