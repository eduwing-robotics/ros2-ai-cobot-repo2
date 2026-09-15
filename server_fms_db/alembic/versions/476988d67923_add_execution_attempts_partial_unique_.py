"""Add execution_attempts partial unique indexes for attempt_no"""
revision = '476988d67923'
down_revision = '5a7815347783'
branch_labels = None
depends_on = None
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_index(
        'uq_execution_attempts_job_step_command_attempt',
        'execution_attempts',
        ['job_step_id', 'command_type', 'attempt_no'],
        unique=True,
        postgresql_where=sa.text('job_step_id IS NOT NULL')
    )
    op.create_index(
        'uq_execution_attempts_job_delivery_command_attempt',
        'execution_attempts',
        ['job_delivery_id', 'command_type', 'attempt_no'],
        unique=True,
        postgresql_where=sa.text('job_delivery_id IS NOT NULL')
    )


def downgrade():
    op.drop_index('uq_execution_attempts_job_delivery_command_attempt', table_name='execution_attempts')
    op.drop_index('uq_execution_attempts_job_step_command_attempt', table_name='execution_attempts')
