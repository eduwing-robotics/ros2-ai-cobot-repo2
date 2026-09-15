"""Add nullable roof option code to production jobs.

Revision ID: 20260812_01
Revises: 20260808_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260812_01"
down_revision = "20260808_01"
branch_labels = None
depends_on = None

_ROOF_OPTION_CODE = postgresql.ENUM(
    "ROOF_01",
    "ROOF_02",
    name="roof_option_code",
)


def upgrade() -> None:
    _ROOF_OPTION_CODE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "production_jobs",
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
    )


def downgrade() -> None:
    op.drop_column("production_jobs", "roof_option_code")
    _ROOF_OPTION_CODE.drop(op.get_bind(), checkfirst=True)
