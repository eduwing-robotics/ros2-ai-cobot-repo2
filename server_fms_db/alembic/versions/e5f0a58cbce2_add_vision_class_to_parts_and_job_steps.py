"""Add vision_class to parts and job_steps"""
revision = 'e5f0a58cbce2'
down_revision = 'bdb85ee80d7f'
branch_labels = None
depends_on = None
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column('parts', sa.Column('vision_class', sa.String(length=50), nullable=True))
    op.add_column('job_steps', sa.Column('vision_class', sa.String(length=50), nullable=True))

def downgrade() -> None:
    op.drop_column('job_steps', 'vision_class')
    op.drop_column('parts', 'vision_class')
