"""Fail-closed safety helpers for the local disposable rehearsal database."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

REHEARSAL_DATABASE_NAME = "smart_factory_rehearsal"
OPERATOR_GATE_REHEARSAL_DATABASE_NAME = "smart_factory_operator_gate_rehearsal"
PRODUCTION_DATABASE_NAME = "smart_factory_db"
BENCHMARK_DATABASE_NAME = "smart_factory_benchmark"


class RehearsalDatabaseSafetyError(RuntimeError):
    pass


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _validated_rehearsal_database_url(
    *, environment: Mapping[str, str], variable_name: str, expected_database_name: str
) -> str:
    """Validate one explicitly named, loopback-only disposable database URL."""
    target_url = environment.get(variable_name, "").strip()
    if not target_url:
        raise RehearsalDatabaseSafetyError(f"{variable_name} is required.")
    try:
        target = make_url(target_url)
    except Exception as exc:
        raise RehearsalDatabaseSafetyError(f"{variable_name} must be a valid SQLAlchemy URL.") from exc
    if target.get_backend_name() != "postgresql":
        raise RehearsalDatabaseSafetyError("Rehearsal database must use PostgreSQL.")
    if target.database != expected_database_name:
        raise RehearsalDatabaseSafetyError(
            f"Rehearsal target must be exactly {expected_database_name!r}."
        )
    if not _is_loopback(target.host):
        raise RehearsalDatabaseSafetyError("Rehearsal database host must be loopback-only.")
    for configured_name in (
        "DATABASE_URL",
        "POSTGRES_TEST_DATABASE_URL",
        "FACTORY_REHEARSAL_DATABASE_URL",
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL",
    ):
        configured = environment.get(configured_name, "").strip()
        if configured and configured_name != variable_name and configured == target_url:
            raise RehearsalDatabaseSafetyError(
                f"{variable_name} must differ from {configured_name}."
            )
    return target_url


def rehearsal_database_url(*, environment: Mapping[str, str]) -> str:
    """Validate the PRE_ROOF/full-stack rehearsal URL without connecting."""
    return _validated_rehearsal_database_url(
        environment=environment,
        variable_name="FACTORY_REHEARSAL_DATABASE_URL",
        expected_database_name=REHEARSAL_DATABASE_NAME,
    )


def operator_gate_rehearsal_database_url(*, environment: Mapping[str, str]) -> str:
    """Validate the dedicated operator-gate GUI rehearsal URL without connecting."""
    return _validated_rehearsal_database_url(
        environment=environment,
        variable_name="OPERATOR_GATE_REHEARSAL_DATABASE_URL",
        expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    )

def verified_session_factory(
    *, database_url: str, expected_database_name: str = REHEARSAL_DATABASE_NAME
) -> sessionmaker[Session]:
    """Connect only to the already-validated rehearsal DB and verify its name."""
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            actual = connection.scalar(text("SELECT current_database()"))
        if actual != expected_database_name:
            raise RehearsalDatabaseSafetyError(
                f"Connected database {actual!r} is not {expected_database_name!r}."
            )
    except Exception:
        engine.dispose()
        raise
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def administration_url(
    *, database_url: str, expected_database_name: str = REHEARSAL_DATABASE_NAME
) -> URL:
    """Return the local maintenance-db URL for explicit CREATE DATABASE only."""
    target = make_url(database_url)
    if target.database != expected_database_name or not _is_loopback(target.host):
        raise RehearsalDatabaseSafetyError("Only the configured loopback rehearsal target may derive an admin URL.")
    return target.set(database="postgres")
