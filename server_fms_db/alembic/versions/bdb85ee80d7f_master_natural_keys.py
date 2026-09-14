"""master_natural_keys"""
revision = 'bdb85ee80d7f'
down_revision = 'e8f2dbf47163'
branch_labels = None
depends_on = None
from alembic import op
import sqlalchemy as sa


def upgrade():
    # 1. Add new natural key columns as nullable
    op.add_column('assembly_recipe_stages', sa.Column('part_code', sa.String(length=50), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('slot_code', sa.String(length=50), nullable=True))
    op.add_column('installation_slots', sa.Column('product_code', sa.String(length=50), nullable=True))
    op.add_column('inventory', sa.Column('part_code', sa.String(length=50), nullable=True))
    op.add_column('inventory_movements', sa.Column('part_code', sa.String(length=50), nullable=True))
    op.add_column('job_material_delivery_items', sa.Column('part_code', sa.String(length=50), nullable=True))
    op.add_column('production_jobs', sa.Column('product_code', sa.String(length=50), nullable=True))

    # 2. Backfill data
    op.execute("UPDATE installation_slots SET product_code = products.product_code FROM products WHERE installation_slots.product_id = products.product_id")
    op.execute("UPDATE production_jobs SET product_code = products.product_code FROM products WHERE production_jobs.product_id = products.product_id")

    # 3. Alter columns to NOT NULL where required
    # Note: parts table is empty so we don't strictly need to backfill inventory/movements/etc., but we will set them to NOT NULL.
    # Installation slots might be empty too, but backfill is safe.
    # assembly_recipe_stages part/slot were nullable, so they remain nullable.
    op.alter_column('production_jobs', 'product_code', nullable=False)
    # installation_slots product_code nullable=False
    op.execute("DELETE FROM installation_slots WHERE product_code IS NULL")
    op.alter_column('installation_slots', 'product_code', nullable=False)

    # 4. Drop old FKs
    op.drop_constraint('fk_recipe_stage_slot', 'assembly_recipe_stages', type_='foreignkey')
    op.drop_constraint('fk_recipe_stage_part', 'assembly_recipe_stages', type_='foreignkey')
    op.drop_constraint('installation_slots_product_id_fkey', 'installation_slots', type_='foreignkey')
    op.drop_constraint('inventory_part_id_fkey', 'inventory', type_='foreignkey')
    op.drop_constraint('inventory_movements_part_id_fkey', 'inventory_movements', type_='foreignkey')
    op.drop_constraint('job_material_delivery_items_part_id_fkey', 'job_material_delivery_items', type_='foreignkey')
    op.drop_constraint('fk_job_step_part', 'job_steps', type_='foreignkey')
    op.drop_constraint('fk_job_step_slot', 'job_steps', type_='foreignkey')
    op.drop_constraint('production_jobs_product_id_fkey', 'production_jobs', type_='foreignkey')

    # 5. Drop old PK constraints & unique constraints
    op.drop_constraint('products_pkey', 'products', type_='primary')
    op.drop_constraint('products_product_code_key', 'products', type_='unique')

    op.drop_constraint('parts_pkey', 'parts', type_='primary')
    op.drop_constraint('parts_part_code_key', 'parts', type_='unique')

    op.drop_constraint('inventory_pkey', 'inventory', type_='primary')

    op.drop_constraint('installation_slots_pkey', 'installation_slots', type_='primary')
    # Actually wait, uq_installation_slots_slot_code might be needed to drop before promoting PK
    op.drop_constraint('uq_installation_slots_slot_code', 'installation_slots', type_='unique')

    op.execute('ALTER TABLE job_material_delivery_items DROP CONSTRAINT IF EXISTS uq_job_material_delivery_items_step_part')

    # 6. Create new Primary Keys
    op.create_primary_key('products_pkey', 'products', ['product_code'])
    op.create_primary_key('parts_pkey', 'parts', ['part_code'])
    op.create_primary_key('installation_slots_pkey', 'installation_slots', ['slot_code'])
    op.create_primary_key('inventory_pkey', 'inventory', ['part_code'])

    # 7. Create new Foreign Keys & Unique Constraints
    op.create_foreign_key('fk_recipe_stage_part', 'assembly_recipe_stages', 'parts', ['part_code'], ['part_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_recipe_stage_slot', 'assembly_recipe_stages', 'installation_slots', ['slot_code'], ['slot_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_assembly_recipe_product', 'assembly_recipes', 'products', ['product_code'], ['product_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_installation_slot_product', 'installation_slots', 'products', ['product_code'], ['product_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_inventory_part', 'inventory', 'parts', ['part_code'], ['part_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_inventory_movement_part', 'inventory_movements', 'parts', ['part_code'], ['part_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_delivery_item_part', 'job_material_delivery_items', 'parts', ['part_code'], ['part_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_pending_request_product', 'pending_production_requests', 'products', ['product_code'], ['product_code'], ondelete='RESTRICT')
    op.create_foreign_key('fk_production_job_product', 'production_jobs', 'products', ['product_code'], ['product_code'], ondelete='RESTRICT')

    op.create_unique_constraint('uq_job_material_delivery_items_step_part', 'job_material_delivery_items', ['job_delivery_id', 'job_step_id', 'part_code'])

    op.execute('ALTER TABLE installation_slots DROP CONSTRAINT IF EXISTS ck_installation_slots_code_nonblank')
    op.create_check_constraint('ck_installation_slots_code_nonblank', 'installation_slots', 'length(trim(slot_code)) > 0')

    # 8. Drop old columns
    op.drop_column('assembly_recipe_stages', 'part_id')
    op.drop_column('assembly_recipe_stages', 'installation_slot_id')
    op.drop_column('installation_slots', 'product_id')
    op.drop_column('installation_slots', 'installation_slot_id')
    op.drop_column('inventory', 'part_id')
    op.drop_column('inventory_movements', 'part_id')
    op.drop_column('job_material_delivery_items', 'part_id')
    op.drop_column('job_steps', 'part_id')
    op.drop_column('job_steps', 'installation_slot_id')
    op.drop_column('production_jobs', 'product_id')
    op.drop_column('parts', 'part_id')
    op.drop_column('products', 'product_id')

def downgrade():
    # 1. Add back surrogate IDs
    op.add_column('products', sa.Column('product_id', sa.INTEGER(), autoincrement=True, nullable=True))
    op.add_column('parts', sa.Column('part_id', sa.INTEGER(), autoincrement=True, nullable=True))
    op.add_column('installation_slots', sa.Column('installation_slot_id', sa.INTEGER(), autoincrement=True, nullable=True))
    op.add_column('installation_slots', sa.Column('product_id', sa.INTEGER(), nullable=True))

    op.add_column('assembly_recipe_stages', sa.Column('part_id', sa.INTEGER(), nullable=True))
    op.add_column('assembly_recipe_stages', sa.Column('installation_slot_id', sa.INTEGER(), nullable=True))
    op.add_column('inventory', sa.Column('part_id', sa.INTEGER(), nullable=True))
    op.add_column('inventory_movements', sa.Column('part_id', sa.INTEGER(), nullable=True))
    op.add_column('job_material_delivery_items', sa.Column('part_id', sa.INTEGER(), nullable=True))
    op.add_column('job_steps', sa.Column('part_id', sa.INTEGER(), nullable=True))
    op.add_column('job_steps', sa.Column('installation_slot_id', sa.INTEGER(), nullable=True))
    op.add_column('production_jobs', sa.Column('product_id', sa.INTEGER(), nullable=True))

    # 2. Re-assign surrogate IDs starting from 1 (simplified backfill)
    op.execute("WITH numbered AS (SELECT product_code, row_number() over () as new_id FROM products) UPDATE products SET product_id = numbered.new_id FROM numbered WHERE products.product_code = numbered.product_code")
    op.alter_column('products', 'product_id', nullable=False)

    # Parts (assuming 0 parts or just sequence)
    op.execute("WITH numbered AS (SELECT part_code, row_number() over () as new_id FROM parts) UPDATE parts SET part_id = numbered.new_id FROM numbered WHERE parts.part_code = numbered.part_code")

    # 3. Backfill old FKs using JOINs
    op.execute("UPDATE installation_slots SET product_id = products.product_id FROM products WHERE installation_slots.product_code = products.product_code")
    op.execute("UPDATE production_jobs SET product_id = products.product_id FROM products WHERE production_jobs.product_code = products.product_code")
    op.alter_column('production_jobs', 'product_id', nullable=False)

    # Drop new FK constraints
    op.drop_constraint('fk_recipe_stage_part', 'assembly_recipe_stages', type_='foreignkey')
    op.drop_constraint('fk_recipe_stage_slot', 'assembly_recipe_stages', type_='foreignkey')
    op.drop_constraint('fk_assembly_recipe_product', 'assembly_recipes', type_='foreignkey')
    op.drop_constraint('fk_installation_slot_product', 'installation_slots', type_='foreignkey')
    op.drop_constraint('fk_inventory_part', 'inventory', type_='foreignkey')
    op.drop_constraint('fk_inventory_movement_part', 'inventory_movements', type_='foreignkey')
    op.drop_constraint('fk_delivery_item_part', 'job_material_delivery_items', type_='foreignkey')
    op.drop_constraint('fk_pending_request_product', 'pending_production_requests', type_='foreignkey')
    op.drop_constraint('fk_production_job_product', 'production_jobs', type_='foreignkey')

    # Drop new PKs
    op.drop_constraint('products_pkey', 'products', type_='primary')
    op.drop_constraint('parts_pkey', 'parts', type_='primary')
    op.drop_constraint('installation_slots_pkey', 'installation_slots', type_='primary')
    op.drop_constraint('inventory_pkey', 'inventory', type_='primary')

    op.execute('ALTER TABLE job_material_delivery_items DROP CONSTRAINT IF EXISTS uq_job_material_delivery_items_step_part')
    op.drop_constraint('ck_installation_slots_code_nonblank', 'installation_slots', type_='check')

    # Restore surrogate PKs
    op.create_primary_key('products_pkey', 'products', ['product_id'])
    op.create_primary_key('parts_pkey', 'parts', ['part_id'])
    op.create_primary_key('installation_slots_pkey', 'installation_slots', ['installation_slot_id'])
    op.create_primary_key('inventory_pkey', 'inventory', ['part_id'])

    # Restore UniqueConstraints
    op.create_unique_constraint('products_product_code_key', 'products', ['product_code'])
    op.create_unique_constraint('parts_part_code_key', 'parts', ['part_code'])
    op.create_unique_constraint('uq_job_material_delivery_items_step_part', 'job_material_delivery_items', ['job_delivery_id', 'job_step_id', 'part_id'])
    op.create_unique_constraint('uq_installation_slots_slot_code', 'installation_slots', ['slot_code'])

    # Restore old FKs
    op.create_foreign_key('fk_recipe_stage_part', 'assembly_recipe_stages', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_recipe_stage_slot', 'assembly_recipe_stages', 'installation_slots', ['installation_slot_id'], ['installation_slot_id'], ondelete='RESTRICT')
    op.create_foreign_key('installation_slots_product_id_fkey', 'installation_slots', 'products', ['product_id'], ['product_id'], ondelete='RESTRICT')
    op.create_foreign_key('inventory_part_id_fkey', 'inventory', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('inventory_movements_part_id_fkey', 'inventory_movements', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('job_material_delivery_items_part_id_fkey', 'job_material_delivery_items', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_job_step_part', 'job_steps', 'parts', ['part_id'], ['part_id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_job_step_slot', 'job_steps', 'installation_slots', ['installation_slot_id'], ['installation_slot_id'], ondelete='RESTRICT')
    op.create_foreign_key('production_jobs_product_id_fkey', 'production_jobs', 'products', ['product_id'], ['product_id'], ondelete='RESTRICT')

    # Drop new columns
    op.drop_column('assembly_recipe_stages', 'part_code')
    op.drop_column('assembly_recipe_stages', 'slot_code')
    op.drop_column('installation_slots', 'product_code')
    op.drop_column('inventory', 'part_code')
    op.drop_column('inventory_movements', 'part_code')
    op.drop_column('job_material_delivery_items', 'part_code')
    op.drop_column('production_jobs', 'product_code')
