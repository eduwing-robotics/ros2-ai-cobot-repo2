"""Add job_steps.failed_at and job_material_deliveries.failure_reason"""
revision = '5aa0e4bd35e8'
down_revision = 'b1fcb137c6d8'
branch_labels = None
depends_on = None
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('job_material_deliveries', sa.Column('failure_reason', sa.Text(), nullable=True))
    op.add_column('job_steps', sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))

    # Backfill job_steps.failed_at using production_events
    op.execute("""
        UPDATE job_steps
        SET failed_at = pe.created_at
        FROM production_events pe
        WHERE job_steps.job_step_id = pe.job_step_id
          AND job_steps.status = 'FAILED'
          AND pe.event_type = 'STEP_FAILED'
          AND job_steps.failed_at IS NULL
    """)

def downgrade():
    op.drop_column('job_steps', 'failed_at')
    op.drop_column('job_material_deliveries', 'failure_reason')
