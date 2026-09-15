"""Create confirmed factory schema and reference seed data.

Revision ID: 20260808_01
Revises: None
"""
from alembic import op
import sqlalchemy as sa
from shared.models import Base
revision = "20260808_01"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("""CREATE TYPE part_category AS ENUM ('STRUCTURE', 'BATHROOM', 'KITCHEN');
CREATE TYPE job_status AS ENUM ('REQUESTED', 'READY', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELED');
CREATE TYPE inventory_movement_type AS ENUM ('IN', 'OUT', 'ADJUST');
CREATE TYPE job_step_status AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED');
CREATE TYPE inspection_status AS ENUM ('PASS', 'FAIL');
CREATE TYPE production_event_type AS ENUM ('JOB_CREATED', 'JOB_STARTED', 'JOB_PAUSED', 'JOB_RESUMED', 'JOB_CANCELED', 'JOB_COMPLETED', 'JOB_FAILED', 'STEP_STARTED', 'STEP_COMPLETED', 'STEP_FAILED', 'INSPECTION_PASSED', 'INSPECTION_FAILED', 'ROBOT_ERROR', 'ROBOT_ERROR_CLEARED', 'ROBOT_DISCONNECTED', 'ROBOT_RECONNECTED');
CREATE TYPE ai_input_type AS ENUM ('TEXT', 'VOICE');
CREATE TYPE ai_intent AS ENUM ('CREATE_PRODUCTION_REQUEST', 'PAUSE_JOB', 'RESUME_JOB', 'CANCEL_JOB', 'QUERY_JOB_STATUS', 'QUERY_INVENTORY', 'UNKNOWN');

CREATE TABLE products (
	product_id SERIAL NOT NULL,
	product_code VARCHAR(50) NOT NULL,
	product_name VARCHAR(150) NOT NULL,
	description TEXT,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (product_id),
	UNIQUE (product_code)
)

;

CREATE TABLE parts (
	part_id SERIAL NOT NULL,
	part_code VARCHAR(50) NOT NULL,
	part_name VARCHAR(150) NOT NULL,
	category part_category NOT NULL,
	unit VARCHAR(20) NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (part_id),
	UNIQUE (part_code)
)

;

CREATE TABLE equipment (
	equipment_id SERIAL NOT NULL,
	equipment_code VARCHAR(50) NOT NULL,
	equipment_name VARCHAR(150) NOT NULL,
	equipment_type VARCHAR(50) NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (equipment_id),
	UNIQUE (equipment_code)
)

;

CREATE TABLE product_bom_items (
	bom_item_id SERIAL NOT NULL,
	product_id INTEGER NOT NULL,
	part_id INTEGER NOT NULL,
	required_quantity INTEGER NOT NULL,
	PRIMARY KEY (bom_item_id),
	CONSTRAINT uq_product_bom_product_part UNIQUE (product_id, part_id),
	FOREIGN KEY(product_id) REFERENCES products (product_id) ON DELETE RESTRICT,
	FOREIGN KEY(part_id) REFERENCES parts (part_id) ON DELETE RESTRICT
)

;

CREATE TABLE product_part_layouts (
	layout_id SERIAL NOT NULL,
	product_id INTEGER NOT NULL,
	part_id INTEGER NOT NULL,
	instance_no INTEGER NOT NULL,
	position_x NUMERIC(12, 3) NOT NULL,
	position_y NUMERIC(12, 3) NOT NULL,
	position_z NUMERIC(12, 3) NOT NULL,
	rotation_roll NUMERIC(10, 4) NOT NULL,
	rotation_pitch NUMERIC(10, 4) NOT NULL,
	rotation_yaw NUMERIC(10, 4) NOT NULL,
	coordinate_frame VARCHAR(100) NOT NULL,
	position_tolerance NUMERIC(10, 3) NOT NULL,
	rotation_tolerance NUMERIC(10, 4) NOT NULL,
	PRIMARY KEY (layout_id),
	CONSTRAINT uq_layout_product_part_instance UNIQUE (product_id, part_id, instance_no),
	FOREIGN KEY(product_id) REFERENCES products (product_id) ON DELETE RESTRICT,
	FOREIGN KEY(part_id) REFERENCES parts (part_id) ON DELETE RESTRICT
)

;

CREATE TABLE process_steps (
	process_step_id SERIAL NOT NULL,
	step_order INTEGER NOT NULL,
	step_code VARCHAR(80) NOT NULL,
	step_name VARCHAR(150) NOT NULL,
	equipment_id INTEGER NOT NULL,
	PRIMARY KEY (process_step_id),
	UNIQUE (step_order),
	UNIQUE (step_code),
	FOREIGN KEY(equipment_id) REFERENCES equipment (equipment_id) ON DELETE RESTRICT
)

;

CREATE TABLE inventory (
	part_id INTEGER NOT NULL,
	quantity INTEGER NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (part_id),
	FOREIGN KEY(part_id) REFERENCES parts (part_id) ON DELETE RESTRICT
)

;

CREATE TABLE production_jobs (
	job_id SERIAL NOT NULL,
	job_code VARCHAR(80) NOT NULL,
	product_id INTEGER NOT NULL,
	status job_status NOT NULL,
	requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	failure_reason TEXT,
	PRIMARY KEY (job_id),
	UNIQUE (job_code),
	FOREIGN KEY(product_id) REFERENCES products (product_id) ON DELETE RESTRICT
)

;

CREATE TABLE inventory_movements (
	movement_id SERIAL NOT NULL,
	part_id INTEGER NOT NULL,
	job_id INTEGER,
	movement_type inventory_movement_type NOT NULL,
	quantity INTEGER NOT NULL,
	reason TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (movement_id),
	FOREIGN KEY(part_id) REFERENCES parts (part_id) ON DELETE RESTRICT,
	FOREIGN KEY(job_id) REFERENCES production_jobs (job_id) ON DELETE SET NULL
)

;

CREATE TABLE job_steps (
	job_step_id SERIAL NOT NULL,
	job_id INTEGER NOT NULL,
	process_step_id INTEGER NOT NULL,
	status job_step_status NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	failure_reason TEXT,
	PRIMARY KEY (job_step_id),
	CONSTRAINT uq_job_process_step UNIQUE (job_id, process_step_id),
	FOREIGN KEY(job_id) REFERENCES production_jobs (job_id) ON DELETE RESTRICT,
	FOREIGN KEY(process_step_id) REFERENCES process_steps (process_step_id) ON DELETE RESTRICT
)

;

CREATE TABLE inspection_results (
	inspection_result_id SERIAL NOT NULL,
	job_id INTEGER NOT NULL,
	layout_id INTEGER NOT NULL,
	detected BOOLEAN NOT NULL,
	detected_x NUMERIC(12, 3),
	detected_y NUMERIC(12, 3),
	detected_z NUMERIC(12, 3),
	position_error NUMERIC(10, 3),
	result inspection_status NOT NULL,
	inspected_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (inspection_result_id),
	FOREIGN KEY(job_id) REFERENCES production_jobs (job_id) ON DELETE RESTRICT,
	FOREIGN KEY(layout_id) REFERENCES product_part_layouts (layout_id) ON DELETE RESTRICT
)

;

CREATE TABLE ai_requests (
	ai_request_id SERIAL NOT NULL,
	input_type ai_input_type NOT NULL,
	input_text TEXT NOT NULL,
	intent ai_intent NOT NULL,
	job_id INTEGER,
	success BOOLEAN NOT NULL,
	failure_reason TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (ai_request_id),
	FOREIGN KEY(job_id) REFERENCES production_jobs (job_id) ON DELETE SET NULL
)

;

CREATE TABLE production_events (
	event_id SERIAL NOT NULL,
	job_id INTEGER,
	job_step_id INTEGER,
	equipment_id INTEGER,
	event_type production_event_type NOT NULL,
	error_code VARCHAR(100),
	message TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (event_id),
	FOREIGN KEY(job_id) REFERENCES production_jobs (job_id) ON DELETE SET NULL,
	FOREIGN KEY(job_step_id) REFERENCES job_steps (job_step_id) ON DELETE SET NULL,
	FOREIGN KEY(equipment_id) REFERENCES equipment (equipment_id) ON DELETE SET NULL
)

;
"""))
    bind.execute(sa.text("""INSERT INTO products (product_code, product_name, description, is_active) VALUES
      ('HOUSE_A','A형 초소형 하우스','A형 초소형 하우스',true),
      ('HOUSE_B','B형 초소형 하우스','B형 초소형 하우스',true)
      ON CONFLICT (product_code) DO NOTHING"""))


def downgrade():

    bind = op.get_bind()
    bind.execute(sa.text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))
