"""PostgreSQL connection primitives.

ORM models and Alembic migrations define the schema; this module still does not connect at API startup.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings

logger = logging.getLogger(__name__)


def _database_url() -> str:
    database_url = get_settings().database_url.strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is empty. Set a PostgreSQL URL before using database helpers.")
    return database_url


@lru_cache
def get_engine() -> Engine:
    """Create a PostgreSQL engine lazily; no connection is opened here."""

    return create_engine(_database_url(), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return a process-local session factory for future API/FMS database access."""

    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def initialize_database() -> None:
    """Log configuration only; schema changes are applied with Alembic, never at startup."""

    if not get_settings().database_url.strip():
        logger.info("DATABASE_URL is empty; database initialization is skipped.")
        return
    logger.info("Database configuration is present; run `alembic upgrade head` to apply schema migrations.")


def close_database() -> None:
    """Dispose the lazily created engine if this process created one."""

    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        logger.info("Database engine disposed.")
