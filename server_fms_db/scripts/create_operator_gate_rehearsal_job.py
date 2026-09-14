#!/usr/bin/env python3
"""Create one REQUESTED Job in the dedicated operator-gate rehearsal DB."""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rehearsal_database import (
    OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    RehearsalDatabaseSafetyError,
    operator_gate_rehearsal_database_url,
    verified_session_factory,
)
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE
from shared.config import get_settings
from shared.models.factory import RoofOptionCode
from shared.services.production_orchestration_service import ProductionOrchestrationService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-code")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = operator_gate_rehearsal_database_url(environment={
        "DATABASE_URL": settings.database_url,
        "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
        "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
        "OPERATOR_GATE_REHEARSAL_DATABASE_URL": settings.operator_gate_rehearsal_database_url,
    })
    factory = verified_session_factory(
        database_url=target,
        expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    )
    try:
        job_code = args.job_code or f"OPERATOR_GATE_HOUSE_B_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
        with factory() as session:
            job = ProductionOrchestrationService(session).create_job(
                product_code=HOUSE_B_PRODUCT_CODE,
                job_code=job_code,
                roof_option_code=RoofOptionCode.ROOF_02,
            )
            print(f"OPERATOR GATE REHEARSAL JOB CREATED: job_id={job.job_id} job_code={job.job_code} status={job.status.value}")
        return 0
    finally:
        factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except RehearsalDatabaseSafetyError as exc:
        print(f"OPERATOR_GATE_REHEARSAL_JOB_CREATE_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
