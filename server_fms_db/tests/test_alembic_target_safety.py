from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.alembic_target_safety import (
    OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    PRODUCTION_DATABASE_NAME,
    REHEARSAL_DATABASE_NAME,
    TEST_DATABASE_NAME,
    AlembicTargetSafetyError,
    resolve_alembic_target,
    validate_connected_target,
    validate_offline_target,
)


PRODUCTION_URL = "postgresql+psycopg://prod_user:prod_secret@db.example/smart_factory_db"
TEST_URL = "postgresql+psycopg://test_user:test_secret@db.example/smart_factory_benchmark"
REHEARSAL_URL = "postgresql+psycopg://rehearsal_user:rehearsal_secret@127.0.0.1/smart_factory_rehearsal"
OPERATOR_GATE_REHEARSAL_URL = "postgresql+psycopg://operator_user:operator_secret@127.0.0.1/smart_factory_operator_gate_rehearsal"


def resolve(*, environment: dict[str, str], application_url: str = PRODUCTION_URL, test_url: str = TEST_URL, rehearsal_url: str = REHEARSAL_URL, operator_gate_rehearsal_url: str = OPERATOR_GATE_REHEARSAL_URL):
    return resolve_alembic_target(
        environ=environment,
        application_database_url=application_url,
        postgres_test_database_url=test_url,
        rehearsal_database_url=rehearsal_url,
        operator_gate_rehearsal_database_url=operator_gate_rehearsal_url,
    )


def test_missing_explicit_target_never_falls_back_to_database_url():
    with pytest.raises(AlembicTargetSafetyError, match="DATABASE_URL is not used as a fallback"):
        resolve(environment={"ALEMBIC_ENV": "test"})


def test_test_target_requires_exact_configured_test_url_and_live_database_name():
    target = resolve(
        environment={"ALEMBIC_ENV": "test", "ALEMBIC_DATABASE_URL": TEST_URL}
    )

    validate_connected_target(target, TEST_DATABASE_NAME)
    validate_offline_target(target)


def test_test_mode_blocks_a_production_target_before_migration_context():
    with pytest.raises(AlembicTargetSafetyError, match="must exactly match"):
        resolve(
            environment={"ALEMBIC_ENV": "test", "ALEMBIC_DATABASE_URL": PRODUCTION_URL}
        )


def test_test_mode_blocks_wrong_connected_database_name():
    target = resolve(
        environment={"ALEMBIC_ENV": "test", "ALEMBIC_DATABASE_URL": TEST_URL}
    )

    with pytest.raises(AlembicTargetSafetyError, match="connected to"):
        validate_connected_target(target, PRODUCTION_DATABASE_NAME)



def test_rehearsal_target_requires_exact_configured_local_target_and_is_nonproduction():
    target = resolve(
        environment={"ALEMBIC_ENV": "rehearsal", "ALEMBIC_DATABASE_URL": REHEARSAL_URL}
    )

    assert target.production_allowed is False
    validate_connected_target(target, REHEARSAL_DATABASE_NAME)
    validate_offline_target(target)


def test_operator_gate_rehearsal_target_requires_its_exact_explicit_url_and_is_nonproduction():
    target = resolve(environment={
        "ALEMBIC_ENV": "operator_gate_rehearsal",
        "ALEMBIC_DATABASE_URL": OPERATOR_GATE_REHEARSAL_URL,
    })
    assert target.production_allowed is False
    validate_connected_target(target, OPERATOR_GATE_REHEARSAL_DATABASE_NAME)
    validate_offline_target(target)


def test_operator_gate_rehearsal_mode_rejects_existing_rehearsal_alias_before_connection():
    with pytest.raises(AlembicTargetSafetyError, match="must exactly match"):
        resolve(environment={
            "ALEMBIC_ENV": "operator_gate_rehearsal",
            "ALEMBIC_DATABASE_URL": REHEARSAL_URL,
        })


def test_rehearsal_mode_blocks_benchmark_or_production_aliases_before_connection():
    with pytest.raises(AlembicTargetSafetyError, match="must exactly match"):
        resolve(environment={"ALEMBIC_ENV": "rehearsal", "ALEMBIC_DATABASE_URL": TEST_URL})
    with pytest.raises(AlembicTargetSafetyError, match="must target"):
        resolve(
            environment={"ALEMBIC_ENV": "rehearsal", "ALEMBIC_DATABASE_URL": "postgresql+psycopg://x:y@127.0.0.1/not_rehearsal"},
            rehearsal_url="postgresql+psycopg://x:y@127.0.0.1/not_rehearsal",
        )

def test_production_target_is_blocked_without_explicit_approval():
    with pytest.raises(AlembicTargetSafetyError, match="ALLOW_PRODUCTION_ALEMBIC=1"):
        resolve(
            environment={"ALEMBIC_ENV": "production", "ALEMBIC_DATABASE_URL": PRODUCTION_URL}
        )


def test_production_target_is_blocked_with_wrong_confirmation():
    with pytest.raises(AlembicTargetSafetyError, match="ALEMBIC_CONFIRM_DATABASE"):
        resolve(
            environment={
                "ALEMBIC_ENV": "production",
                "ALEMBIC_DATABASE_URL": PRODUCTION_URL,
                "ALLOW_PRODUCTION_ALEMBIC": "1",
                "ALEMBIC_CONFIRM_DATABASE": TEST_DATABASE_NAME,
            }
        )


def test_production_target_correct_approval_passes_pure_validation_only():
    target = resolve(
        environment={
            "ALEMBIC_ENV": "production",
            "ALEMBIC_DATABASE_URL": PRODUCTION_URL,
            "ALLOW_PRODUCTION_ALEMBIC": "1",
            "ALEMBIC_CONFIRM_DATABASE": PRODUCTION_DATABASE_NAME,
        }
    )

    validate_connected_target(target, PRODUCTION_DATABASE_NAME)
    with pytest.raises(AlembicTargetSafetyError, match="Offline production Alembic is prohibited"):
        validate_offline_target(target)


def test_same_application_and_test_database_config_is_blocked():
    with pytest.raises(AlembicTargetSafetyError, match="must be distinct"):
        resolve(
            environment={"ALEMBIC_ENV": "test", "ALEMBIC_DATABASE_URL": TEST_URL},
            application_url=TEST_URL,
        )


def test_safety_errors_never_echo_database_credentials():
    with pytest.raises(AlembicTargetSafetyError) as raised:
        resolve(
            environment={"ALEMBIC_ENV": "test", "ALEMBIC_DATABASE_URL": PRODUCTION_URL}
        )

    assert "prod_secret" not in str(raised.value)
    assert PRODUCTION_URL not in str(raised.value)


def test_default_alembic_cli_fails_before_any_database_connection_when_target_is_unset():
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": PRODUCTION_URL,
            "POSTGRES_TEST_DATABASE_URL": TEST_URL,
        }
    )
    environment.pop("ALEMBIC_DATABASE_URL", None)
    environment.pop("ALEMBIC_ENV", None)
    environment.pop("ALLOW_PRODUCTION_ALEMBIC", None)
    environment.pop("ALEMBIC_CONFIRM_DATABASE", None)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Alembic target database URL must be explicitly set" in output
    assert "prod_secret" not in output
    assert "test_secret" not in output
