from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_MIGRATION = Path("alembic/versions/20260908_01_pending_collecting_details.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("pending_collecting_details_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def scalar(self):
        return 1


class _Bind:
    class dialect:
        name = "sqlite"

    def execute(self, _statement):
        return _Result()


class _Op:
    def __init__(self):
        self.drop_index_calls = 0

    def get_bind(self):
        return _Bind()

    def drop_index(self, *_args, **_kwargs):
        self.drop_index_calls += 1
        raise AssertionError("downgrade must stop before schema/index DDL")


def test_downgrade_blocks_null_product_code_before_not_null_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    fake_op = _Op()
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="product_code IS NULL"):
        migration.downgrade()
    assert fake_op.drop_index_calls == 0


class _NoRowsResult:
    def scalar(self): return None


class _PostgresBind:
    class dialect:
        name = "postgresql"

    def execute(self, _statement):
        return _NoRowsResult()


class _Autocommit:
    def __init__(self, events): self.events = events
    def __enter__(self): self.events.append("autocommit-enter")
    def __exit__(self, *_args): self.events.append("autocommit-exit")


class _PostgresUpgradeOp:
    def __init__(self): self.events = []
    def get_bind(self): return _PostgresBind()
    def get_context(self):
        owner = self
        class _Context:
            def autocommit_block(self): return _Autocommit(owner.events)
        return _Context()
    def execute(self, statement): self.events.append(str(statement))
    def drop_index(self, *_args, **_kwargs): self.events.append("drop-index")
    def batch_alter_table(self, *_args, **_kwargs):
        owner = self
        class _Batch:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def alter_column(self, *_args, **_kwargs): owner.events.append("alter-nullable")
        return _Batch()
    def create_index(self, *_args, **kwargs):
        self.events.append(("create-index", str(kwargs.get("postgresql_where"))))


def test_postgres_enum_add_commits_before_partial_index_uses_new_value(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    fake_op = _PostgresUpgradeOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    add_index = next(i for i, event in enumerate(fake_op.events) if isinstance(event, str) and "ADD VALUE IF NOT EXISTS" in event)
    commit_index = fake_op.events.index("autocommit-exit")
    create_index = next(i for i, event in enumerate(fake_op.events) if isinstance(event, tuple) and event[0] == "create-index")
    assert fake_op.events.index("autocommit-enter") < add_index < commit_index < create_index
    assert "COLLECTING_DETAILS" in fake_op.events[create_index][1]
    assert "WAITING_ROOF_OPTION" in fake_op.events[create_index][1]
    assert "AWAITING_CONFIRMATION" in fake_op.events[create_index][1]
    assert "alter-nullable" in fake_op.events


def test_revision_lineage_is_unchanged() -> None:
    migration = _load_migration()
    assert migration.revision == "20260908_01"
    assert migration.down_revision == "20260907_02"
