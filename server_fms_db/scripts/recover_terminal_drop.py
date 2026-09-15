"""Benchmark-only repair for one terminal Job whose successful pallet return was never recorded."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.services.terminal_drop_recovery_service import (
    TerminalDropAlreadyRecovered,
    TerminalDropRecoveryError,
    TerminalDropRecoveryService,
)

_BENCHMARK_DATABASE = "smart_factory_benchmark"


def _require_benchmark_identity(*, url: URL, actual_database: str | None) -> None:
    if (
        not url.drivername.startswith("postgresql")
        or url.database != _BENCHMARK_DATABASE
        or actual_database != _BENCHMARK_DATABASE
    ):
        raise RuntimeError("BENCHMARK_TERMINAL_DROP_RECOVERY_ABORTED_WRONG_DATABASE")


def _print_identity(url: URL) -> None:
    print(f"host = {url.host}")
    print(f"port = {url.port}")
    print(f"database = {url.database}")


def _print_assessment(assessment) -> None:
    print(f"job_id = {assessment.job_id}")
    print(f"job_status = {assessment.job_status.value}")
    print(f"delivery_id = {assessment.delivery_id}")
    print(f"supply_group = {assessment.supply_group_code}")
    print(f"forward_attempt = {assessment.forward_attempt_id}")
    print(f"forward_status = {assessment.forward_status.value}")
    print(f"current_drop_state = {assessment.drop_state.value}")
    print(f"current_drop_owner = {assessment.drop_owner_delivery_id}")
    print("existing_empty_return = NONE")
    print("recovery_eligible = YES")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--delivery-id", type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate only (the default).")
    mode.add_argument("--execute", action="store_true", help="Persist the approved recovery.")
    args = parser.parse_args(argv)

    url = make_url(get_settings().postgres_test_database_url.strip())
    # Parse-time guard occurs before any connection and never reads DATABASE_URL.
    if url.database != _BENCHMARK_DATABASE or not url.drivername.startswith("postgresql"):
        print("BENCHMARK_TERMINAL_DROP_RECOVERY_ABORTED_WRONG_DATABASE", file=sys.stderr)
        return 2
    _print_identity(url)
    engine = create_engine(url, pool_pre_ping=False)
    try:
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            actual_database = session.scalar(text("SELECT current_database()"))
            try:
                _require_benchmark_identity(url=url, actual_database=actual_database)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            service = TerminalDropRecoveryService(session)
            try:
                assessment = service.assess(job_id=args.job_id, delivery_id=args.delivery_id)
                _print_assessment(assessment)
                if not args.execute:
                    session.rollback()
                    return 0
                result = service.recover(job_id=args.job_id, delivery_id=args.delivery_id)
                print("recovery_status = SUCCESS")
                print(f"recovery_attempt_id = {result.attempt_id}")
                print(f"recovery_req_id = {result.req_id}")
                return 0
            except TerminalDropAlreadyRecovered as exc:
                session.rollback()
                print(f"recovery_status = ALREADY_RECOVERED ({exc})")
                return 0
            except TerminalDropRecoveryError as exc:
                session.rollback()
                print(f"recovery_status = ABORTED ({exc})", file=sys.stderr)
                return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
