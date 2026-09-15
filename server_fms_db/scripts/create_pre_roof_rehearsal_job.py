#!/usr/bin/env python3
"""Create one isolated rehearsal Job through ProductionOrchestrationService.

This intentionally stops at REQUESTED.  It never modifies Job status directly;
advance the job to PRE_ROOF_READY through the normal API/FMS/Fake boundaries
before invoking the PRE_ROOF process rehearsal.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rehearsal_database import RehearsalDatabaseSafetyError, rehearsal_database_url, verified_session_factory
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE
from shared.config import get_settings
from shared.models.factory import RoofOptionCode
from shared.services.production_orchestration_service import ProductionOrchestrationService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-code", help="Optional unique rehearsal job code.")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = rehearsal_database_url(environment={
        "DATABASE_URL": settings.database_url,
        "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
        "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
    })
    factory = verified_session_factory(database_url=target)
    try:
        job_code = args.job_code or f"REHEARSAL_HOUSE_B_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
        with factory() as session:
            job = ProductionOrchestrationService(session).create_job(
                product_code=HOUSE_B_PRODUCT_CODE,
                job_code=job_code,
                roof_option_code=RoofOptionCode.ROOF_02,
            )
            print(f"REHEARSAL JOB CREATED: job_id={job.job_id} job_code={job.job_code} status={job.status.value}")
        return 0
    finally:
        factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except RehearsalDatabaseSafetyError as exc:
        print(f"REHEARSAL_JOB_CREATE_FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
