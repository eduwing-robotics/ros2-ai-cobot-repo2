"""collapse recipe schema

Revision ID: 034f64cfe733
Revises: 8e243d30f480
Create Date: 2026-08-14 17:48:26.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '034f64cfe733'
down_revision = '8e243d30f480'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Drop legacy tables
    op.drop_table('assembly_recipe_stage_part_installation_slots')
    op.drop_table('product_roof_option_part_installation_slots')
    op.drop_table('assembly_recipe_stage_parts')
    op.drop_table('product_roof_option_parts')
    op.drop_table('product_bom_items')

    # Add simplified columns to assembly_recipe_stages
    op.add_column('assembly_recipe_stages', sa.Column('part_id', sa.Integer(), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('quantity', sa.Integer(), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('installation_slot_id', sa.Integer(), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('pick_zone', sa.String(length=100), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('option_code', sa.String(length=100), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('execution_gate', sa.String(length=100), nullable=True))

    op.create_foreign_key('fk_stage_part', 'assembly_recipe_stages', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_stage_slot', 'assembly_recipe_stages', 'installation_slots', ['installation_slot_id'], ['installation_slot_id'], ondelete='RESTRICT')

    # Add simplified columns to job_steps
    op.add_column('job_steps', sa.Column('part_id', sa.Integer(), nullable=True))
    op.add_column('job_steps', sa.Column('quantity', sa.Integer(), nullable=True))
    op.add_column('job_steps', sa.Column('installation_slot_id', sa.Integer(), nullable=True))
    op.add_column('job_steps', sa.Column('pick_zone', sa.String(length=100), nullable=True))

    op.create_foreign_key('fk_jobstep_part', 'job_steps', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_jobstep_slot', 'job_steps', 'installation_slots', ['installation_slot_id'], ['installation_slot_id'], ondelete='RESTRICT')

def downgrade() -> None:
    pass
