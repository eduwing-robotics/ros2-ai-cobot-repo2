#!/usr/bin/env python3
"""Create (only with explicit confirmation), migrate, and seed the rehearsal DB.

No benchmark or production database is selected.  This tool never drops a
schema/database and never creates operational jobs.
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
    REHEARSAL_DATABASE_NAME,
    RehearsalDatabaseSafetyError,
    administration_url,
    rehearsal_database_url,
    verified_session_factory,
)
from scripts.seed_house_b_mvp_master import seed_house_b_mvp_master
from shared.config import get_settings

EXPECTED_REVISION = "20260907_02"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-database", action="store_true", help="Create smart_factory_rehearsal only if missing.")
    parser.add_argument("--confirm-database", help="Required exact confirmation with --create-database.")
    return parser.parse_args(argv)


def _ensure_database_exists(*, database_url: str, create: bool, confirmation: str | None) -> None:
    if not create:
        try:
            factory = verified_session_factory(database_url=database_url)
        except OperationalError as exc:
            raise RehearsalDatabaseSafetyError(
                "Rehearsal database does not exist or cannot be reached; rerun with --create-database "
                "and --confirm-database smart_factory_rehearsal after reviewing local PostgreSQL access."
            ) from exc
        else:
            factory.kw["bind"].dispose()
            return
    if confirmation != REHEARSAL_DATABASE_NAME:
        raise RehearsalDatabaseSafetyError(
            "Database creation requires --confirm-database smart_factory_rehearsal exactly."
        )
    engine = create_engine(administration_url(database_url=database_url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": REHEARSAL_DATABASE_NAME})
            if not exists:
                # Identifier is a source constant, never user-provided SQL.
                connection.execute(text('CREATE DATABASE "smart_factory_rehearsal"'))
    finally:
        engine.dispose()


def _migrate(*, database_url: str) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "FACTORY_REHEARSAL_DATABASE_URL": database_url,
            "ALEMBIC_ENV": "rehearsal",
            "ALEMBIC_DATABASE_URL": database_url,
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RehearsalDatabaseSafetyError("Alembic rehearsal migration failed:\n" + result.stderr.strip())


def _revision(factory) -> str:
    with factory() as session:
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    return str(revision or "")


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = rehearsal_database_url(environment={
        "DATABASE_URL": settings.database_url,
        "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
        "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
    })
    _ensure_database_exists(database_url=target, create=args.create_database, confirmation=args.confirm_database)
    _migrate(database_url=target)
    factory = verified_session_factory(database_url=target)
    try:
        if _revision(factory) != EXPECTED_REVISION:
            raise RehearsalDatabaseSafetyError(
                f"Expected Alembic {EXPECTED_REVISION} after upgrade; no job or seed was written."
            )
        with factory() as session:
            recipe = seed_house_b_mvp_master(session)
            session.commit()
        print(f"REHEARSAL DB READY: {REHEARSAL_DATABASE_NAME} recipe_id={recipe.recipe_id} revision={EXPECTED_REVISION}")
        return 0
    finally:
        factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except RehearsalDatabaseSafetyError as exc:
        print(f"REHEARSAL_DB_SETUP_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
