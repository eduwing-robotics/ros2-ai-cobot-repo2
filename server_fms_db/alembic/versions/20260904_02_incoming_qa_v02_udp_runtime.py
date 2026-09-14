"""Add durable Incoming QA v0.2 UDP runtime evidence.

Revision ID: 20260904_02
Revises: 20260904_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_02"
down_revision = "20260904_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL enum values are global type metadata.  The autocommit block is
    # required so ERROR is immediately usable by the following runtime state.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE incoming_qa_transaction_status ADD VALUE IF NOT EXISTS 'ERROR'")

    op.add_column("incoming_qa_transactions", sa.Column("error_reason", sa.Text(), nullable=True))
    op.add_column("incoming_qa_transactions", sa.Column("result_snapshot_json", sa.Text(), nullable=True))
    op.add_column("incoming_qa_transactions", sa.Column("camera_source", sa.String(length=100), nullable=True))
    op.add_column("incoming_qa_transactions", sa.Column("vision_timestamp", sa.DateTime(timezone=True), nullable=True))
    op.add_column("incoming_qa_transactions", sa.Column("model_scope", sa.String(length=100), nullable=True))
    op.add_column("incoming_qa_transactions", sa.Column("model_version", sa.String(length=100), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    evidence = bind.execute(sa.text("""
        SELECT count(*)
        FROM incoming_qa_transactions
        WHERE status = 'ERROR'
           OR error_reason IS NOT NULL
           OR result_snapshot_json IS NOT NULL
           OR camera_source IS NOT NULL
           OR vision_timestamp IS NOT NULL
           OR model_scope IS NOT NULL
           OR model_version IS NOT NULL
    """)).scalar_one()
    if evidence:
        raise RuntimeError(
            "Cannot downgrade Incoming QA v0.2 UDP runtime while runtime evidence exists."
        )

    op.drop_column("incoming_qa_transactions", "model_version")
    op.drop_column("incoming_qa_transactions", "model_scope")
    op.drop_column("incoming_qa_transactions", "vision_timestamp")
    op.drop_column("incoming_qa_transactions", "camera_source")
    op.drop_column("incoming_qa_transactions", "result_snapshot_json")
    op.drop_column("incoming_qa_transactions", "error_reason")

    # PostgreSQL cannot remove an enum value. Rebuild the type only after the
    # guard above proves no ERROR-valued row exists.
    op.execute("CREATE TYPE incoming_qa_transaction_status_previous AS ENUM ('REQUESTED', 'SENT', 'ACKED', 'COMPLETED', 'REJECTED')")
    op.execute("""
        ALTER TABLE incoming_qa_transactions
        ALTER COLUMN status TYPE incoming_qa_transaction_status_previous
        USING status::text::incoming_qa_transaction_status_previous
    """)
    op.execute("DROP TYPE incoming_qa_transaction_status")
    op.execute("ALTER TYPE incoming_qa_transaction_status_previous RENAME TO incoming_qa_transaction_status")
