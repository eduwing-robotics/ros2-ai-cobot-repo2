"""Quarantine explicitly selected stale benchmark Jobs without deleting history.

This command is intentionally benchmark-only.  It requires DATABASE_URL to point to
``smart_factory_benchmark`` and verifies the actual target with
``SELECT current_database()`` before either listing or changing Jobs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    JobStatus,
    JobStep,
    ProductionJob,
    StepStatus,
)
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionOrchestrationService,
)

BENCHMARK_DATABASE_NAME = "smart_factory_benchmark"
DEFAULT_CANCELLATION_REASON = "Benchmark stale test/demo job quarantine."


class BenchmarkCleanupSafetyError(RuntimeError):
    """Raised before a command could target a non-benchmark database."""


@dataclass(frozen=True)
class StaleJobCandidate:
    job_id: int
    job_code: str
    product_code: str
    status: JobStatus
    requested_at: datetime
    started_at: datetime | None
    pending_step_count: int
    missing_vision_class_count: int
    active_attempt_count: int


def _parse_before(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--before must be an ISO-8601 timestamp with an explicit timezone."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "--before must include an explicit timezone, for example 2026-09-04T00:00:00+09:00."
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List or explicitly cancel stale RUNNING Jobs in smart_factory_benchmark."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="List candidates only (default).")
    mode.add_argument("--apply", action="store_true", help="Cancel only explicitly selected candidates.")
    parser.add_argument(
        "--job-id",
        action="append",
        type=int,
        dest="job_ids",
        help="Explicit RUNNING Job ID to select. May be supplied more than once.",
    )
    parser.add_argument(
        "--before",
        type=_parse_before,
        help="Select only Jobs requested before this timezone-aware ISO-8601 timestamp.",
    )
    parser.add_argument(
        "--reason",
        default=DEFAULT_CANCELLATION_REASON,
        help="Audit reason recorded in the JOB_CANCELED event when --apply is used.",
    )
    return parser


def require_benchmark_database_name(database_name: str) -> None:
    if database_name != BENCHMARK_DATABASE_NAME:
        raise BenchmarkCleanupSafetyError(
            "Refusing benchmark stale-job cleanup: "
            f"current_database()={database_name!r}, expected {BENCHMARK_DATABASE_NAME!r}."
        )


def _require_benchmark_database(engine: Engine) -> None:
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
    require_benchmark_database_name(database_name)


def _open_benchmark_session_factory() -> tuple[Engine, sessionmaker[Session]]:
    database_url = get_settings().database_url.strip()
    if not database_url:
        raise BenchmarkCleanupSafetyError(
            "DATABASE_URL must be explicitly configured for benchmark stale-job cleanup."
        )
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        _require_benchmark_database(engine)
    except Exception:
        engine.dispose()
        raise
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def list_stale_running_jobs(
    session: Session,
    *,
    job_ids: Sequence[int] | None = None,
    before: datetime | None = None,
) -> list[StaleJobCandidate]:
    """Return RUNNING candidates without changing any durable state.

    Supplying both selectors narrows the result (logical AND), which prevents an
    accidental broad apply.  Supplying neither is permitted only for dry-run
    inventory output.
    """

    pending_steps = (
        select(func.count(JobStep.job_step_id))
        .where(
            JobStep.job_id == ProductionJob.job_id,
            JobStep.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
        )
        .correlate(ProductionJob)
        .scalar_subquery()
    )
    missing_vision_class = (
        select(func.count(JobStep.job_step_id))
        .where(
            JobStep.job_id == ProductionJob.job_id,
            or_(JobStep.vision_class.is_(None), func.trim(JobStep.vision_class) == ""),
        )
        .correlate(ProductionJob)
        .scalar_subquery()
    )
    active_attempts = (
        select(func.count(ExecutionAttempt.attempt_id))
        .where(
            ExecutionAttempt.job_id == ProductionJob.job_id,
            ExecutionAttempt.status.in_(
                [
                    ExecutionAttemptStatus.CREATED,
                    ExecutionAttemptStatus.DISPATCHING,
                    ExecutionAttemptStatus.ACCEPTED,
                    ExecutionAttemptStatus.UNKNOWN,
                ]
            ),
        )
        .correlate(ProductionJob)
        .scalar_subquery()
    )
    statement = (
        select(
            ProductionJob.job_id,
            ProductionJob.job_code,
            ProductionJob.product_code,
            ProductionJob.status,
            ProductionJob.requested_at,
            ProductionJob.started_at,
            pending_steps.label("pending_step_count"),
            missing_vision_class.label("missing_vision_class_count"),
            active_attempts.label("active_attempt_count"),
        )
        .where(ProductionJob.status == JobStatus.RUNNING)
        .order_by(ProductionJob.requested_at.asc(), ProductionJob.job_id.asc())
    )
    if job_ids:
        statement = statement.where(ProductionJob.job_id.in_(sorted(set(job_ids))))
    if before is not None:
        statement = statement.where(ProductionJob.requested_at < before)

    return [
        StaleJobCandidate(
            job_id=row.job_id,
            job_code=row.job_code,
            product_code=row.product_code,
            status=row.status,
            requested_at=row.requested_at,
            started_at=row.started_at,
            pending_step_count=row.pending_step_count,
            missing_vision_class_count=row.missing_vision_class_count,
            active_attempt_count=row.active_attempt_count,
        )
        for row in session.execute(statement)
    ]


def cancel_selected_jobs(
    session: Session,
    candidates: Sequence[StaleJobCandidate],
    *,
    reason: str,
) -> list[int]:
    """Use the canonical Job lifecycle service to quarantine selected Jobs.

    A fresh row lock and terminal-state check occurs for every Job.  JobSteps,
    deliveries, and attempts are intentionally not rewritten or deleted.
    """

    orchestration = ProductionOrchestrationService(session)
    applied_ids: list[int] = []
    for candidate in candidates:
        if candidate.active_attempt_count:
            raise BenchmarkCleanupSafetyError(
                f"Refusing to cancel job_id={candidate.job_id}: it has "
                f"{candidate.active_attempt_count} unresolved execution attempt(s). "
                "Stop or reconcile physical execution through its own authority first."
            )
        try:
            orchestration.cancel_job(candidate.job_id, reason=reason)
        except InvalidProductionStateTransitionError:
            # Another operator/worker changed the candidate after dry-run/listing.
            # Do not override that newer authoritative state.
            continue
        applied_ids.append(candidate.job_id)
    return applied_ids


def _print_candidates(candidates: Sequence[StaleJobCandidate]) -> None:
    if not candidates:
        print("No matching RUNNING benchmark Job candidates.")
        return
    for candidate in candidates:
        print(
            "job_id={job_id} product_code={product_code} status={status} "
            "requested_at={requested_at} started_at={started_at} "
            "pending_step_count={pending_step_count} "
            "missing_vision_class_count={missing_vision_class_count} "
            "active_attempt_count={active_attempt_count}".format(
                job_id=candidate.job_id,
                product_code=candidate.product_code,
                status=candidate.status.value,
                requested_at=candidate.requested_at.isoformat(),
                started_at=candidate.started_at.isoformat() if candidate.started_at else "<none>",
                pending_step_count=candidate.pending_step_count,
                missing_vision_class_count=candidate.missing_vision_class_count,
                active_attempt_count=candidate.active_attempt_count,
            )
        )
    print(f"candidate_count={len(candidates)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply and not args.job_ids and args.before is None:
        raise BenchmarkCleanupSafetyError(
            "--apply requires at least one explicit selector: --job-id and/or --before."
        )

    engine, session_factory = _open_benchmark_session_factory()
    try:
        with session_factory() as session:
            candidates = list_stale_running_jobs(
                session, job_ids=args.job_ids, before=args.before
            )
            _print_candidates(candidates)
            if not args.apply:
                print("DRY_RUN_ONLY: no ProductionJob state was changed.")
                return 0

            applied_ids = cancel_selected_jobs(session, candidates, reason=args.reason)
            print("applied_job_ids=" + (",".join(map(str, applied_ids)) or "<none>"))
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkCleanupSafetyError as error:
        print(f"REFUSED: {error}")
        raise SystemExit(2) from error
