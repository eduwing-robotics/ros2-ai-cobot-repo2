"""Quarantine one explicitly selected stale Incoming QA inspection in benchmark.

This emergency tool deliberately never falls back to ``DATABASE_URL``.  The
caller must pass the dedicated benchmark URL explicitly and the tool verifies
``current_database()`` before it reads or writes anything.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionStatus,
    ProductionJob,
)
from shared.services.material_inspection_service import MaterialInspectionService


BENCHMARK_DATABASE_NAME = "smart_factory_benchmark"
DEFAULT_QUARANTINE_REASON = "BENCHMARK_STALE_INCOMING_QA_QUARANTINE"
_ACTIVE_INSPECTION_STATUSES = {
    MaterialInspectionStatus.REQUESTED,
    MaterialInspectionStatus.RUNNING,
}
_ALLOWED_TRANSACTION_STATUSES = {
    IncomingQATransactionStatus.ERROR,
    IncomingQATransactionStatus.REJECTED,
}


class BenchmarkIncomingQACleanupSafetyError(RuntimeError):
    """Raised before an unsafe stale Incoming QA cleanup could proceed."""


@dataclass(frozen=True)
class StaleIncomingQACandidate:
    inspection_id: int
    inspection_request_id: str
    inspection_cycle: int
    inspection_status: MaterialInspectionStatus
    result: str | None
    production_valid: bool | None
    delivery_item_id: int
    expected_part_code: str
    expected_class_name: str
    job_id: int
    job_code: str
    transaction_id: int | None
    transaction_status: IncomingQATransactionStatus | None


def require_benchmark_database_name(database_name: str) -> None:
    if database_name != BENCHMARK_DATABASE_NAME:
        raise BenchmarkIncomingQACleanupSafetyError(
            "Refusing benchmark stale Incoming QA cleanup: "
            f"current_database()={database_name!r}, expected {BENCHMARK_DATABASE_NAME!r}."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or quarantine one exact stale Incoming QA inspection in smart_factory_benchmark."
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="Explicit dedicated benchmark PostgreSQL URL; DATABASE_URL is never read or used.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (default).")
    mode.add_argument("--apply", action="store_true", help="Apply terminal quarantine to one exact active row.")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--inspection-id", type=int, help="Exact MaterialInspection primary key.")
    selector.add_argument("--request-id", help="Exact inspection_request_id; must resolve to one item.")
    parser.add_argument(
        "--reason",
        default=DEFAULT_QUARANTINE_REASON,
        help="Persisted terminal error reason used only with --apply.",
    )
    return parser


def _open_benchmark_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    if not database_url.strip():
        raise BenchmarkIncomingQACleanupSafetyError("--database-url must be non-empty.")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            require_benchmark_database_name(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
    except Exception:
        engine.dispose()
        raise
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def find_candidates(
    session: Session,
    *,
    inspection_id: int | None = None,
    request_id: str | None = None,
) -> list[StaleIncomingQACandidate]:
    if inspection_id is None and not request_id:
        raise BenchmarkIncomingQACleanupSafetyError(
            "An exact --inspection-id or --request-id is required; broad stale-row selection is forbidden."
        )

    statement = (
        select(MaterialInspection)
        .order_by(MaterialInspection.inspection_id)
    )
    if inspection_id is not None:
        statement = statement.where(MaterialInspection.inspection_id == inspection_id)
    if request_id:
        statement = statement.where(MaterialInspection.inspection_request_id == request_id)

    candidates: list[StaleIncomingQACandidate] = []
    for inspection in session.scalars(statement):
        item = session.get(JobMaterialDeliveryItem, inspection.delivery_item_id)
        delivery = session.get(JobMaterialDelivery, item.job_delivery_id) if item else None
        job = session.get(ProductionJob, delivery.production_job_id) if delivery else None
        transaction = (
            session.get(IncomingQATransaction, inspection.incoming_qa_transaction_id)
            if inspection.incoming_qa_transaction_id is not None
            else None
        )
        if item is None or delivery is None or job is None:
            raise BenchmarkIncomingQACleanupSafetyError(
                f"Inspection {inspection.inspection_id} has broken delivery/job references; refusing quarantine."
            )
        candidates.append(
            StaleIncomingQACandidate(
                inspection_id=inspection.inspection_id,
                inspection_request_id=inspection.inspection_request_id,
                inspection_cycle=inspection.inspection_cycle,
                inspection_status=inspection.status,
                result=None if inspection.result is None else inspection.result.value,
                production_valid=inspection.production_valid,
                delivery_item_id=inspection.delivery_item_id,
                expected_part_code=inspection.expected_part_code,
                expected_class_name=inspection.expected_class_name,
                job_id=job.job_id,
                job_code=job.job_code,
                transaction_id=None if transaction is None else transaction.transaction_id,
                transaction_status=None if transaction is None else transaction.status,
            )
        )
    return candidates


def quarantine_candidate(
    session: Session,
    candidate: StaleIncomingQACandidate,
    *,
    reason: str,
) -> int:
    """Terminalize exactly one stale active item through the existing service.

    ``mark_error`` is the canonical MaterialInspection terminal transition.  A
    v0.2 multi-item request is deliberately refused: it requires transaction
    level resolution rather than a single-row emergency action.  For a v0.2
    row, the associated transaction must already be terminal ERROR/REJECTED.
    """

    inspection = session.scalar(
        select(MaterialInspection)
        .where(MaterialInspection.inspection_id == candidate.inspection_id)
        .with_for_update()
    )
    if inspection is None:
        raise BenchmarkIncomingQACleanupSafetyError(
            f"Inspection {candidate.inspection_id} disappeared before quarantine."
        )
    if inspection.inspection_request_id != candidate.inspection_request_id:
        raise BenchmarkIncomingQACleanupSafetyError("Inspection request identity changed; refusing quarantine.")
    if inspection.status not in _ACTIVE_INSPECTION_STATUSES:
        raise BenchmarkIncomingQACleanupSafetyError(
            f"Inspection {inspection.inspection_id} is no longer active ({inspection.status.value}); no mutation applied."
        )

    item_count = session.scalar(
        select(func.count(MaterialInspection.inspection_id)).where(
            MaterialInspection.inspection_request_id == inspection.inspection_request_id
        )
    )
    if item_count != 1:
        raise BenchmarkIncomingQACleanupSafetyError(
            "Refusing multi-item request quarantine; transaction-level recovery is required."
        )

    if inspection.incoming_qa_transaction_id is not None:
        transaction = session.get(IncomingQATransaction, inspection.incoming_qa_transaction_id)
        if transaction is None or transaction.status not in _ALLOWED_TRANSACTION_STATUSES:
            status = "<missing>" if transaction is None else transaction.status.value
            raise BenchmarkIncomingQACleanupSafetyError(
                "Refusing v0.2 item quarantine unless its transaction is already "
                f"ERROR or REJECTED; current transaction status={status}."
            )

    transitioned = MaterialInspectionService().mark_error(
        session,
        inspection.inspection_request_id,
        failure_reason=reason,
    )
    if transitioned.inspection_id != inspection.inspection_id:
        raise BenchmarkIncomingQACleanupSafetyError(
            "Canonical transition resolved a different inspection; transaction rolled back."
        )
    session.commit()
    return transitioned.inspection_id


def _print_candidates(candidates: Sequence[StaleIncomingQACandidate]) -> None:
    if not candidates:
        print("No exact benchmark Incoming QA inspection matched.")
        return
    for candidate in candidates:
        transaction = (
            "<none>"
            if candidate.transaction_id is None
            else f"{candidate.transaction_id}/{candidate.transaction_status.value if candidate.transaction_status else '<missing>'}"
        )
        print(
            "inspection_id={inspection_id} request_id={request_id} cycle={cycle} "
            "status={status} result={result} production_valid={production_valid} "
            "delivery_item_id={delivery_item_id} part={part} class={klass} "
            "job_id={job_id} job_code={job_code} transaction={transaction}".format(
                inspection_id=candidate.inspection_id,
                request_id=candidate.inspection_request_id,
                cycle=candidate.inspection_cycle,
                status=candidate.inspection_status.value,
                result=candidate.result,
                production_valid=candidate.production_valid,
                delivery_item_id=candidate.delivery_item_id,
                part=candidate.expected_part_code,
                klass=candidate.expected_class_name,
                job_id=candidate.job_id,
                job_code=candidate.job_code,
                transaction=transaction,
            )
        )
    print(f"candidate_count={len(candidates)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply and args.inspection_id is None and not args.request_id:
        raise BenchmarkIncomingQACleanupSafetyError(
            "--apply requires exactly one explicit --inspection-id or --request-id."
        )
    engine, session_factory = _open_benchmark_session_factory(args.database_url)
    try:
        with session_factory() as session:
            candidates = find_candidates(
                session, inspection_id=args.inspection_id, request_id=args.request_id
            )
            _print_candidates(candidates)
            if not args.apply:
                print("DRY_RUN_ONLY: no MaterialInspection state was changed.")
                return 0
            if len(candidates) != 1:
                raise BenchmarkIncomingQACleanupSafetyError(
                    "--apply requires its exact selector to resolve to exactly one inspection."
                )
            applied_id = quarantine_candidate(session, candidates[0], reason=args.reason)
            print(f"quarantined_inspection_id={applied_id}")
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkIncomingQACleanupSafetyError as error:
        print(f"REFUSED: {error}")
        raise SystemExit(2) from error
