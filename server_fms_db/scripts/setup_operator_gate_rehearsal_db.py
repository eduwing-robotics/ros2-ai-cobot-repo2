#!/usr/bin/env python3
"""Create, migrate, and seed only the dedicated operator-gate rehearsal DB.

This command never drops/reset databases and requires an exact confirmation before
creating its loopback-only PostgreSQL target.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rehearsal_database import (
    OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    RehearsalDatabaseSafetyError,
    administration_url,
    operator_gate_rehearsal_database_url,
    verified_session_factory,
)
from scripts.seed_house_b_mvp_master import seed_house_b_mvp_master
from shared.config import get_settings

EXPECTED_REVISION = "20260907_02"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--confirm-database")
    return parser.parse_args(argv)


def _ensure_database_exists(*, database_url: str, create: bool, confirmation: str | None) -> None:
    if not create:
        try:
            factory = verified_session_factory(
                database_url=database_url,
                expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
            )
        except OperationalError as exc:
            raise RehearsalDatabaseSafetyError(
                "Dedicated rehearsal database is unavailable; rerun with --create-database "
                "and the exact --confirm-database value after reviewing local PostgreSQL access."
            ) from exc
        else:
            factory.kw["bind"].dispose()
            return
    if confirmation != OPERATOR_GATE_REHEARSAL_DATABASE_NAME:
        raise RehearsalDatabaseSafetyError(
            f"Database creation requires --confirm-database {OPERATOR_GATE_REHEARSAL_DATABASE_NAME} exactly."
        )
    engine = create_engine(
        administration_url(
            database_url=database_url,
            expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
        ),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": OPERATOR_GATE_REHEARSAL_DATABASE_NAME},
            )
            if not exists:
                connection.execute(text('CREATE DATABASE "smart_factory_operator_gate_rehearsal"'))
    finally:
        engine.dispose()


def _migrate(*, database_url: str) -> None:
    environment = dict(os.environ)
    environment.update({
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL": database_url,
        "ALEMBIC_ENV": "operator_gate_rehearsal",
        "ALEMBIC_DATABASE_URL": database_url,
    })
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RehearsalDatabaseSafetyError("Dedicated rehearsal Alembic migration failed:\n" + result.stderr.strip())


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = operator_gate_rehearsal_database_url(environment={
        "DATABASE_URL": settings.database_url,
        "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
        "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL": settings.operator_gate_rehearsal_database_url,
    })
    _ensure_database_exists(
        database_url=target,
        create=args.create_database,
        confirmation=args.confirm_database,
    )
    _migrate(database_url=target)
    factory = verified_session_factory(
        database_url=target,
        expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    )
    try:
        with factory() as session:
            revision = session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != EXPECTED_REVISION:
                raise RehearsalDatabaseSafetyError(
                    f"Expected Alembic {EXPECTED_REVISION} after upgrade; no seed was written."
                )
            recipe = seed_house_b_mvp_master(session)
            session.commit()
        print(f"OPERATOR GATE REHEARSAL DB READY: {OPERATOR_GATE_REHEARSAL_DATABASE_NAME} recipe_id={recipe.recipe_id} revision={EXPECTED_REVISION}")
        return 0
    finally:
        factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except RehearsalDatabaseSafetyError as exc:
        print(f"OPERATOR_GATE_REHEARSAL_DB_SETUP_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
