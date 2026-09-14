from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


_MIGRATION = Path("alembic/versions/20260910_01_durable_voice_pause_immediate.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("durable_voice_pause_immediate_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_initializes_existing_production_jobs_to_non_immediate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE production_jobs (job_id INTEGER PRIMARY KEY, job_code VARCHAR(80))"))
        connection.execute(text("INSERT INTO production_jobs (job_id, job_code) VALUES (1, 'existing')"))
        migration = _load_migration()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert "control_immediate_requested" in {column["name"] for column in inspect(connection).get_columns("production_jobs")}
        assert connection.execute(text("SELECT control_immediate_requested FROM production_jobs WHERE job_id = 1")).scalar_one() in (False, 0)


def test_revision_is_a_single_minimal_child_of_pause_resume_head() -> None:
    migration = _load_migration()
    assert migration.revision == "20260910_01"
    assert migration.down_revision == "20260908_02"
