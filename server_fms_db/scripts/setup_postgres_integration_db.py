#!/usr/bin/env python3
"""Apply the existing Alembic schema only to the dedicated PostgreSQL test DB.

This script intentionally does not create or drop databases. It refuses to run
unless POSTGRES_TEST_DATABASE_URL points exactly to ``smart_factory_benchmark`` and is
different from the development DATABASE_URL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from shared.config import get_settings


EXPECTED_TEST_DATABASE = "smart_factory_benchmark"


def _validate_test_database_url(*, development_url: str, test_url: str) -> None:
    if not test_url:
        raise RuntimeError("POSTGRES_TEST_DATABASE_URL is empty.")

    parsed_test_url = make_url(test_url)
    if parsed_test_url.get_backend_name() != "postgresql":
        raise RuntimeError("POSTGRES_TEST_DATABASE_URL must use PostgreSQL.")
    if parsed_test_url.database != EXPECTED_TEST_DATABASE:
        raise RuntimeError(
            "Refusing to run migrations: POSTGRES_TEST_DATABASE_URL must target "
            f"{EXPECTED_TEST_DATABASE!r}."
        )
    if development_url and test_url == development_url:
        raise RuntimeError(
            "Refusing to run migrations: the test database URL equals DATABASE_URL."
        )


def main() -> int:
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    development_url = settings.database_url.strip()
    _validate_test_database_url(development_url=development_url, test_url=test_url)

    # Alembic never inherits DATABASE_URL. Its administrative target is explicit.
    os.environ["ALEMBIC_ENV"] = "test"
    os.environ["ALEMBIC_DATABASE_URL"] = test_url
    get_settings.cache_clear()

    project_root = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(project_root / "alembic.ini"))

    from shared.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    engine = create_engine(test_url)
    with engine.connect() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO smart_factory_dev;"))
        conn.execute(sa.text("GRANT ALL ON SCHEMA public TO public;"))
        conn.commit()
    Base.metadata.create_all(engine)

    # Temporarily add project root to python path to import tests module
    import sys
    sys.path.insert(0, str(project_root))
    from tests.recipe_test_support import add_active_recipe

    with Session(engine) as session:
        session.execute(sa.text("INSERT INTO products (product_code, product_name, description, is_active) VALUES ('HOUSE_A','A형 초소형 하우스','A형 초소형 하우스',true), ('HOUSE_B','B형 초소형 하우스','B형 초소형 하우스',true) ON CONFLICT (product_code) DO NOTHING"))
        add_active_recipe(session, 'HOUSE_A')
        add_active_recipe(session, 'HOUSE_B')
        session.commit()

    command.stamp(alembic_config, "head")
    print(f"Test schema created and stamped at head in {EXPECTED_TEST_DATABASE}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
