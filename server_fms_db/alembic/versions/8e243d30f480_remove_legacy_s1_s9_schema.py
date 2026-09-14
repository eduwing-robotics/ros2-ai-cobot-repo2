"""remove_legacy_s1_s9_schema"""
revision = '8e243d30f480'
down_revision = '20260814_03'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

def upgrade():
    # 1. Migrate missing step snapshots for legacy job_steps before dropping process_step_id
    op.execute("""
        UPDATE job_steps
        SET step_order = ps.step_order,
            operation_code = ps.step_code,
            display_name = ps.step_name
        FROM process_steps ps
        WHERE job_steps.process_step_id = ps.process_step_id
          AND job_steps.step_order IS NULL;
    """)

    # 2. Drop constraints from job_steps to process_steps and production_events to equipment
    op.execute("ALTER TABLE job_steps DROP CONSTRAINT IF EXISTS job_steps_process_step_id_fkey")
    op.execute("ALTER TABLE production_events DROP CONSTRAINT IF EXISTS production_events_equipment_id_fkey")

    # 3. Drop legacy columns
    op.drop_constraint('uq_job_process_step', 'job_steps', type_='unique')
    op.drop_column('job_steps', 'process_step_id')
    op.drop_column('production_events', 'equipment_id')

    # 4. Drop tables
    op.drop_table('ai_requests')
    op.drop_table('inspection_results')
    op.drop_table('product_part_layouts')
    op.drop_table('process_steps')
    op.drop_table('equipment')

def downgrade():
    # Since this is a legacy cleanup, a perfect downgrade isn't fully possible
    # without data loss (the dropped rows), but we can restore the schema.

    op.create_table('equipment',
        sa.Column('equipment_id', sa.Integer(), primary_key=True),
        sa.Column('equipment_code', sa.String(length=50), unique=True),
        sa.Column('equipment_name', sa.String(length=150)),
        sa.Column('equipment_type', sa.String(length=50)),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )

    op.create_table('process_steps',
        sa.Column('process_step_id', sa.Integer(), primary_key=True),
        sa.Column('step_order', sa.Integer(), unique=True),
        sa.Column('step_code', sa.String(length=80), unique=True),
        sa.Column('step_name', sa.String(length=150)),
        sa.Column('equipment_id', sa.Integer(), sa.ForeignKey('equipment.equipment_id', ondelete='RESTRICT'))
    )

    op.add_column('job_steps', sa.Column('process_step_id', sa.Integer(), nullable=True))
    op.create_foreign_key('job_steps_process_step_id_fkey', 'job_steps', 'process_steps', ['process_step_id'], ['process_step_id'], ondelete='RESTRICT')
    op.create_unique_constraint('uq_job_process_step', 'job_steps', ['job_id', 'process_step_id'])

    op.add_column('production_events', sa.Column('equipment_id', sa.Integer(), nullable=True))
    op.create_foreign_key('production_events_equipment_id_fkey', 'production_events', 'equipment', ['equipment_id'], ['equipment_id'], ondelete='SET NULL')

    op.create_table('product_part_layouts',
        sa.Column('layout_id', sa.Integer(), primary_key=True),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.product_id', ondelete='RESTRICT')),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.part_id', ondelete='RESTRICT')),
        sa.Column('instance_no', sa.Integer()),
        sa.Column('position_x', sa.Numeric(precision=12, scale=3)),
        sa.Column('position_y', sa.Numeric(precision=12, scale=3)),
        sa.Column('position_z', sa.Numeric(precision=12, scale=3)),
        sa.Column('rotation_roll', sa.Numeric(precision=10, scale=4)),
        sa.Column('rotation_pitch', sa.Numeric(precision=10, scale=4)),
        sa.Column('rotation_yaw', sa.Numeric(precision=10, scale=4)),
        sa.Column('coordinate_frame', sa.String(length=100)),
        sa.Column('position_tolerance', sa.Numeric(precision=10, scale=3)),
        sa.Column('rotation_tolerance', sa.Numeric(precision=10, scale=4)),
        sa.UniqueConstraint('product_id', 'part_id', 'instance_no', name='uq_layout_product_part_instance')
    )

    op.create_table('inspection_results',
        sa.Column('inspection_result_id', sa.Integer(), primary_key=True),
        sa.Column('inspection_id', sa.Integer(), sa.ForeignKey('production_inspections.inspection_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('production_jobs.job_id', ondelete='RESTRICT')),
        sa.Column('layout_id', sa.Integer(), sa.ForeignKey('product_part_layouts.layout_id', ondelete='RESTRICT')),
        sa.Column('detected', sa.Boolean()),
        sa.Column('detected_x', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('detected_y', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('detected_z', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('position_error', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('result', sa.String(), nullable=False),
        sa.Column('inspected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )

    op.create_table('ai_requests',
        sa.Column('ai_request_id', sa.Integer(), primary_key=True),
        sa.Column('input_type', sa.String(), nullable=False),
        sa.Column('input_text', sa.Text(), nullable=False),
        sa.Column('intent', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('production_jobs.job_id', ondelete='SET NULL')),
        sa.Column('success', sa.Boolean()),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )
