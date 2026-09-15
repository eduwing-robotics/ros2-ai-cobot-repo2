from __future__ import annotations

import pytest

from scripts.rehearsal_database import (
    OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    REHEARSAL_DATABASE_NAME,
    RehearsalDatabaseSafetyError,
    operator_gate_rehearsal_database_url,
    rehearsal_database_url,
)


REHEARSAL_URL = "postgresql+psycopg://127.0.0.1:5432/smart_factory_rehearsal"


def test_explicit_loopback_rehearsal_target_is_accepted_without_connection() -> None:
    assert rehearsal_database_url(environment={
        "DATABASE_URL": "postgresql+psycopg://db.example/smart_factory_db",
        "POSTGRES_TEST_DATABASE_URL": "postgresql+psycopg://db.example/smart_factory_benchmark",
        "FACTORY_REHEARSAL_DATABASE_URL": REHEARSAL_URL,
    }) == REHEARSAL_URL


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://127.0.0.1:5432/smart_factory_db",
    "postgresql+psycopg://127.0.0.1:5432/smart_factory_benchmark",
    "postgresql+psycopg://192.168.20.20:5432/smart_factory_rehearsal",
])
def test_rehearsal_target_rejects_other_database_or_nonloopback_host(url: str) -> None:
    with pytest.raises(RehearsalDatabaseSafetyError):
        rehearsal_database_url(environment={"FACTORY_REHEARSAL_DATABASE_URL": url})


def test_rehearsal_target_must_not_alias_application_or_benchmark_url() -> None:
    with pytest.raises(RehearsalDatabaseSafetyError, match="must differ"):
        rehearsal_database_url(environment={
            "DATABASE_URL": REHEARSAL_URL,
            "FACTORY_REHEARSAL_DATABASE_URL": REHEARSAL_URL,
        })


def test_rehearsal_name_is_source_constant() -> None:
    assert REHEARSAL_DATABASE_NAME == "smart_factory_rehearsal"


OPERATOR_GATE_REHEARSAL_URL = "postgresql+psycopg://127.0.0.1:5432/smart_factory_operator_gate_rehearsal"

def test_operator_gate_target_accepts_only_its_exact_loopback_url_without_connection() -> None:
    assert operator_gate_rehearsal_database_url(environment={
        "DATABASE_URL": "postgresql+psycopg://db.example/smart_factory_db",
        "POSTGRES_TEST_DATABASE_URL": "postgresql+psycopg://db.example/smart_factory_benchmark",
        "FACTORY_REHEARSAL_DATABASE_URL": REHEARSAL_URL,
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL": OPERATOR_GATE_REHEARSAL_URL,
    }) == OPERATOR_GATE_REHEARSAL_URL


@pytest.mark.parametrize("url", [
    REHEARSAL_URL,
    "postgresql+psycopg://127.0.0.1:5432/smart_factory_db",
    "postgresql+psycopg://127.0.0.1:5432/smart_factory_benchmark",
    "postgresql+psycopg://192.168.20.20:5432/smart_factory_operator_gate_rehearsal",
])
def test_operator_gate_target_rejects_existing_rehearsal_and_all_nonexact_urls(url: str) -> None:
    with pytest.raises(RehearsalDatabaseSafetyError):
        operator_gate_rehearsal_database_url(environment={
            "OPERATOR_GATE_REHEARSAL_DATABASE_URL": url,
            "FACTORY_REHEARSAL_DATABASE_URL": REHEARSAL_URL,
        })


def test_operator_gate_rehearsal_name_is_source_constant() -> None:
    assert OPERATOR_GATE_REHEARSAL_DATABASE_NAME == "smart_factory_operator_gate_rehearsal"
