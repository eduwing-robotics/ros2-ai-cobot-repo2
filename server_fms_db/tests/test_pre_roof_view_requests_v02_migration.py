from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


_MIGRATION = Path("alembic/versions/20260912_01_pre_roof_view_requests_v02.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("pre_roof_view_requests_v02_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_postgres_sql(action: str) -> str:
    migration = _load_migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    migration.op = Operations(context)
    getattr(migration, action)()
    return output.getvalue()


def test_upgrade_reuses_existing_shared_production_inspection_result_enum() -> None:
    sql = _offline_postgres_sql("upgrade")
    assert "CREATE TABLE production_inspection_view_requests" in sql
    assert "result production_inspection_result" in sql
    assert "CREATE TYPE production_inspection_result" not in sql


def test_downgrade_drops_only_the_v02_table_not_shared_enum() -> None:
    sql = _offline_postgres_sql("downgrade")
    assert "DROP TABLE production_inspection_view_requests" in sql
    assert "production_inspection_result" not in sql


def test_revision_chain_continues_from_existing_benchmark_revision() -> None:
    migration = _load_migration()
    assert migration.revision == "20260912_01"
    assert migration.down_revision == "20260910_02"
