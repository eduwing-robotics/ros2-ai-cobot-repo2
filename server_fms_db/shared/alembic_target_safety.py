"""Fail-closed target selection for Alembic commands.

Alembic is an administrative database client, not an application runtime. Its
target must therefore be selected explicitly rather than inherited from
``DATABASE_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.engine import make_url


PRODUCTION_DATABASE_NAME = "smart_factory_db"
TEST_DATABASE_NAME = "smart_factory_benchmark"
REHEARSAL_DATABASE_NAME = "smart_factory_rehearsal"
OPERATOR_GATE_REHEARSAL_DATABASE_NAME = "smart_factory_operator_gate_rehearsal"


class AlembicTargetSafetyError(RuntimeError):
    """Raised before Alembic may enter a migration context."""


@dataclass(frozen=True)
class AlembicTarget:
    """Explicit, validated administrative target configuration."""

    database_url: str
    mode: str
    url_database_name: str
    production_allowed: bool


def _require_database_name(database_url: str, variable_name: str) -> str:
    try:
        database_name = make_url(database_url).database
    except Exception as error:  # SQLAlchemy owns URL parsing details.
        raise AlembicTargetSafetyError(
            f"{variable_name} must be a valid SQLAlchemy database URL."
        ) from error
    if not database_name:
        raise AlembicTargetSafetyError(
            f"{variable_name} must include an explicit database name."
        )
    return database_name


def resolve_alembic_target(
    *,
    environ: Mapping[str, str] | None = None,
    application_database_url: str,
    postgres_test_database_url: str,
    rehearsal_database_url: str = "",
    operator_gate_rehearsal_database_url: str = "",
) -> AlembicTarget:
    """Read only explicit Alembic configuration; never select an app DB by fallback."""

    environment = os.environ if environ is None else environ
    target_url = environment.get("ALEMBIC_DATABASE_URL", "").strip()
    if not target_url:
        raise AlembicTargetSafetyError(
            "Alembic target database URL must be explicitly set via "
            "ALEMBIC_DATABASE_URL. DATABASE_URL is not used as a fallback."
        )

    mode = environment.get("ALEMBIC_ENV", "").strip().lower()
    if mode not in {"test", "rehearsal", "operator_gate_rehearsal", "production"}:
        raise AlembicTargetSafetyError(
            "ALEMBIC_ENV must be explicitly set to test, rehearsal, operator_gate_rehearsal, or production."
        )

    application_url = application_database_url.strip()
    test_url = postgres_test_database_url.strip()
    rehearsal_url = rehearsal_database_url.strip()
    operator_gate_rehearsal_url = operator_gate_rehearsal_database_url.strip()
    configured = {
        "DATABASE_URL": application_url,
        "POSTGRES_TEST_DATABASE_URL": test_url,
        "FACTORY_REHEARSAL_DATABASE_URL": rehearsal_url,
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL": operator_gate_rehearsal_url,
    }
    populated = [(name, value) for name, value in configured.items() if value]
    for index, (left_name, left_url) in enumerate(populated):
        for right_name, right_url in populated[index + 1:]:
            if left_url == right_url:
                raise AlembicTargetSafetyError(
                    f"{left_name} and {right_name} must be distinct before Alembic may run."
                )

    target_database_name = _require_database_name(target_url, "ALEMBIC_DATABASE_URL")
    if mode == "test":
        if not test_url:
            raise AlembicTargetSafetyError(
                "POSTGRES_TEST_DATABASE_URL must be configured for ALEMBIC_ENV=test."
            )
        if target_url != test_url:
            raise AlembicTargetSafetyError(
                "ALEMBIC_DATABASE_URL must exactly match POSTGRES_TEST_DATABASE_URL "
                "when ALEMBIC_ENV=test."
            )
        if target_database_name != TEST_DATABASE_NAME:
            raise AlembicTargetSafetyError(
                f"ALEMBIC_ENV=test must target {TEST_DATABASE_NAME!r}."
            )
        return AlembicTarget(
            database_url=target_url,
            mode=mode,
            url_database_name=target_database_name,
            production_allowed=False,
        )

    if mode == "operator_gate_rehearsal":
        if not operator_gate_rehearsal_url:
            raise AlembicTargetSafetyError(
                "OPERATOR_GATE_REHEARSAL_DATABASE_URL must be configured for "
                "ALEMBIC_ENV=operator_gate_rehearsal."
            )
        if target_url != operator_gate_rehearsal_url:
            raise AlembicTargetSafetyError(
                "ALEMBIC_DATABASE_URL must exactly match OPERATOR_GATE_REHEARSAL_DATABASE_URL "
                "when ALEMBIC_ENV=operator_gate_rehearsal."
            )
        if target_database_name != OPERATOR_GATE_REHEARSAL_DATABASE_NAME:
            raise AlembicTargetSafetyError(
                "ALEMBIC_ENV=operator_gate_rehearsal must target "
                f"{OPERATOR_GATE_REHEARSAL_DATABASE_NAME!r}."
            )
        return AlembicTarget(
            database_url=target_url,
            mode=mode,
            url_database_name=target_database_name,
            production_allowed=False,
        )

    if mode == "rehearsal":
        if not rehearsal_url:
            raise AlembicTargetSafetyError(
                "FACTORY_REHEARSAL_DATABASE_URL must be configured for ALEMBIC_ENV=rehearsal."
            )
        if target_url != rehearsal_url:
            raise AlembicTargetSafetyError(
                "ALEMBIC_DATABASE_URL must exactly match FACTORY_REHEARSAL_DATABASE_URL "
                "when ALEMBIC_ENV=rehearsal."
            )
        if target_database_name != REHEARSAL_DATABASE_NAME:
            raise AlembicTargetSafetyError(
                f"ALEMBIC_ENV=rehearsal must target {REHEARSAL_DATABASE_NAME!r}."
            )
        return AlembicTarget(
            database_url=target_url,
            mode=mode,
            url_database_name=target_database_name,
            production_allowed=False,
        )

    if target_database_name != PRODUCTION_DATABASE_NAME:
        raise AlembicTargetSafetyError(
            f"ALEMBIC_ENV=production must target {PRODUCTION_DATABASE_NAME!r}."
        )
    if environment.get("ALLOW_PRODUCTION_ALEMBIC", "").strip() != "1":
        raise AlembicTargetSafetyError(
            "Production Alembic is blocked. Set ALLOW_PRODUCTION_ALEMBIC=1 only "
            "after the approved production migration procedure."
        )
    if environment.get("ALEMBIC_CONFIRM_DATABASE", "").strip() != PRODUCTION_DATABASE_NAME:
        raise AlembicTargetSafetyError(
            "Production Alembic confirmation failed: ALEMBIC_CONFIRM_DATABASE must "
            f"equal {PRODUCTION_DATABASE_NAME!r}."
        )
    return AlembicTarget(
        database_url=target_url,
        mode=mode,
        url_database_name=target_database_name,
        production_allowed=True,
    )


def validate_connected_target(target: AlembicTarget, actual_database_name: str) -> None:
    """Verify the DB reached by the URL, not merely the URL text."""

    expected_nonproduction = {
        "test": TEST_DATABASE_NAME,
        "rehearsal": REHEARSAL_DATABASE_NAME,
        "operator_gate_rehearsal": OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    }.get(target.mode)
    if expected_nonproduction is not None:
        if actual_database_name != expected_nonproduction:
            raise AlembicTargetSafetyError(
                f"ALEMBIC_ENV={target.mode} connected to {actual_database_name!r}, expected "
                f"{expected_nonproduction!r}."
            )
        return

    if actual_database_name != PRODUCTION_DATABASE_NAME:
        raise AlembicTargetSafetyError(
            f"ALEMBIC_ENV=production connected to {actual_database_name!r}, expected "
            f"{PRODUCTION_DATABASE_NAME!r}."
        )
    if not target.production_allowed:
        raise AlembicTargetSafetyError("Production Alembic approval was not validated.")


def validate_offline_target(target: AlembicTarget) -> None:
    """Offline SQL must not provide a production bypass without a live DB check."""

    if target.mode == "production":
        raise AlembicTargetSafetyError(
            "Offline production Alembic is prohibited because current_database() "
            "cannot verify the target."
        )
    expected = {
        "test": TEST_DATABASE_NAME,
        "rehearsal": REHEARSAL_DATABASE_NAME,
        "operator_gate_rehearsal": OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    }[target.mode]
    if target.url_database_name != expected:
        raise AlembicTargetSafetyError(
            f"Offline {target.mode} Alembic must target {expected!r}."
        )
