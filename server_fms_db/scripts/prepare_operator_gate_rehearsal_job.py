#!/usr/bin/env python3
"""Stop one safe Fake lifecycle exactly at the manual Cell-execution gate.

This rehearsal helper starts from a REQUESTED Job in the dedicated
smart_factory_operator_gate_rehearsal database.
It proves the normal Incoming-QA, manual prestage, FMS, and fake transport
boundaries, then stops immediately after the first transported assembly
Delivery reaches COMPLETED.  It never grants operator execution approval.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from scripts.prepare_pre_roof_rehearsal_job import (
    EXPECTED_REVISION,
    PreparationError,
    _make_worker,
    _prepare_incoming_qa,
    _verify_revision,
    _verify_target_job,
)
from scripts.rehearsal_database import (
    OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
    RehearsalDatabaseSafetyError,
    operator_gate_rehearsal_database_url,
    verified_session_factory,
)
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    ProductionInspection,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.operator_execution_ready_service import (
    is_robot_cell_execution_step,
    operator_execution_ready_required,
)
from shared.services.physical_ready_service import PhysicalReadyService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService


@dataclass(frozen=True)
class OperatorGatePreparationResult:
    job_id: int
    job_code: str
    base_step_id: int
    target_step_id: int
    target_operation_code: str
    target_delivery_ids: tuple[int, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser.parse_args(argv)


def _next_step(factory: sessionmaker[Session], *, job_id: int) -> JobStep | None:
    with factory() as session:
        step = ProductionOrchestrationService(session).get_next_step(job_id)
        if step is not None:
            session.expunge(step)
        return step


def _required_deliveries(session: Session, *, step: JobStep) -> list[JobMaterialDelivery]:
    deliveries = MaterialDeliveryService(session).get_required_deliveries_for_step(step.job_step_id)
    if not deliveries:
        raise PreparationError(
            "MATERIAL_READINESS_BLOCKED",
            f"JobStep {step.job_step_id} has no required MaterialDelivery.",
        )
    if any(
        delivery.supply_mode is not step.supply_mode
        or delivery.supply_group_code != step.supply_group_code
        or delivery.supply_destination_code != step.supply_destination_code
        for delivery in deliveries
    ):
        raise PreparationError(
            "MATERIAL_READINESS_BLOCKED",
            "Required MaterialDelivery policy does not match its JobStep snapshot.",
        )
    return deliveries


def _prepare_base_manual_supply(factory: sessionmaker[Session], *, job_id: int) -> JobStep:
    step = _next_step(factory, job_id=job_id)
    if step is None or step.supply_mode is not SupplyMode.MANUAL:
        raise PreparationError(
            "UNEXPECTED_STAGE",
            "The first executable rehearsal step must use the existing MANUAL base-supply path.",
        )
    with factory() as session:
        stored = session.get(JobStep, step.job_step_id)
        assert stored is not None
        for delivery in _required_deliveries(session, step=stored):
            ManualPrestageService(session).confirm_manual_prestage_ready(
                job_id=job_id,
                job_delivery_id=delivery.job_delivery_id,
                request_id=f"operator-gate-{job_id}-base-manual-{delivery.job_delivery_id}",
            )
    return step


def _prepare_target_physical_supply(
    factory: sessionmaker[Session], *, job_id: int, step: JobStep
) -> tuple[int, ...]:
    if (
        step.status is not StepStatus.PENDING
        or step.supply_mode is not SupplyMode.TRANSPORTED
        or not is_robot_cell_execution_step(step)
    ):
        raise PreparationError(
            "UNEXPECTED_STAGE",
            "First post-base step is not a pending transported Robot Cell assembly step.",
        )
    with factory() as session:
        stored = session.get(JobStep, step.job_step_id)
        assert stored is not None
        deliveries = _required_deliveries(session, step=stored)
        for delivery in deliveries:
            PhysicalReadyService(session).confirm_physical_ready(
                job_delivery_id=delivery.job_delivery_id,
                request_id=f"operator-gate-{job_id}-physical-{delivery.job_delivery_id}",
            )
        return tuple(delivery.job_delivery_id for delivery in deliveries)


def _assert_stop_boundary(
    factory: sessionmaker[Session], *, job_id: int, target_step_id: int,
    delivery_ids: tuple[int, ...], cell: FakeCellActionTransport,
) -> None:
    with factory() as session:
        job = session.get(ProductionJob, job_id)
        step = session.get(JobStep, target_step_id)
        if job is None or job.status is not JobStatus.RUNNING:
            raise PreparationError("TARGET_JOB_INVALID", "Job must remain active/RUNNING at the operator gate.")
        if step is None or step.status is not StepStatus.PENDING or step.supply_mode is not SupplyMode.TRANSPORTED:
            raise PreparationError("UNEXPECTED_STAGE", "Target transported Step did not remain PENDING.")
        if step.operator_execution_ready_at is not None:
            raise PreparationError("UNEXPECTED_OPERATOR_RELEASE", "Target Step received an operator execution release.")
        deliveries = [session.get(JobMaterialDelivery, delivery_id) for delivery_id in delivery_ids]
        if any(delivery is None or delivery.status.value != "COMPLETED" for delivery in deliveries):
            raise PreparationError("MATERIAL_READINESS_BLOCKED", "Target Delivery did not reach COMPLETED.")
        readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
            job_id=job_id, job_step_id=target_step_id
        )
        if readiness.reason is not StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED:
            raise PreparationError(
                "OPERATOR_GATE_NOT_REACHED",
                f"Expected OPERATOR_EXECUTION_READY_REQUIRED, got {readiness.reason!r}.",
            )
        attempt_count = session.scalar(
            select(func.count()).select_from(ExecutionAttempt).where(
                ExecutionAttempt.job_step_id == target_step_id,
                ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
            )
        )
        if attempt_count:
            raise PreparationError("UNEXPECTED_CELL_DISPATCH", "Target Step already has a Robot Cell ExecutionAttempt.")
        if session.scalar(select(func.count()).select_from(ProductionInspection).where(
            ProductionInspection.production_job_id == job_id
        )):
            raise PreparationError("UNEXPECTED_PRE_ROOF", "Preparation reached PRE_ROOF unexpectedly.")
    if len(cell.commands) != 1:
        raise PreparationError("UNEXPECTED_CELL_DISPATCH", "Only the MANUAL base Cell execution may have been dispatched.")


def prepare_job_to_operator_gate(
    factory: sessionmaker[Session], *, job_id: int, timeout_seconds: float = 20.0,
    emit: Callable[[str], None] = print,
) -> OperatorGatePreparationResult:
    if timeout_seconds <= 0:
        raise PreparationError("OPERATOR_GATE_TIMEOUT", "timeout_seconds must be positive.")
    job = _verify_target_job(factory, job_id=job_id)
    emit("Incoming QA     DISPATCHED (loopback Fake Vision v0.2)")
    asyncio.run(_prepare_incoming_qa(factory, job_id=job_id, timeout=timeout_seconds))
    emit("Incoming QA     RELEASED")

    base = _prepare_base_manual_supply(factory, job_id=job_id)
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    worker = _make_worker(factory, cell=cell)
    if not worker.tick():
        raise PreparationError("FAKE_CELL_DISPATCH_TIMEOUT", "FMS did not dispatch the MANUAL base step.")
    with factory() as session:
        base_stored = session.get(JobStep, base.job_step_id)
        if base_stored is None or base_stored.status is not StepStatus.COMPLETED:
            raise PreparationError("STEP_COMPLETION_TIMEOUT", "MANUAL base Step was not completed by Fake Cell.")
    emit(f"{base.operation_code:<20} COMPLETED")

    target = _next_step(factory, job_id=job_id)
    if target is None:
        raise PreparationError("UNEXPECTED_STAGE", "No transported assembly Step follows the MANUAL base Step.")
    delivery_ids = _prepare_target_physical_supply(factory, job_id=job_id, step=target)
    # This FMS tick is deliberately transport-only: readiness is TRANSPORT_PENDING
    # before it, and the Worker owns the Fake forklift delivery transition.
    if not worker.tick():
        raise PreparationError("FAKE_TRANSPORT_DISPATCH_TIMEOUT", "FMS did not dispatch target transported delivery.")
    _assert_stop_boundary(
        factory, job_id=job_id, target_step_id=target.job_step_id,
        delivery_ids=delivery_ids, cell=cell,
    )
    # Explicit restart-safe proof: a later FMS tick cannot create a Cell attempt
    # without the GUI/API operator execution release.
    if worker.tick():
        raise PreparationError("UNEXPECTED_CELL_DISPATCH", "FMS dispatched despite missing operator execution release.")
    _assert_stop_boundary(
        factory, job_id=job_id, target_step_id=target.job_step_id,
        delivery_ids=delivery_ids, cell=cell,
    )
    emit(f"{target.operation_code:<20} DELIVERY COMPLETED")
    emit("Operator execution release NOT granted")
    return OperatorGatePreparationResult(
        job_id=job_id,
        job_code=job.job_code,
        base_step_id=base.job_step_id,
        target_step_id=target.job_step_id,
        target_operation_code=target.operation_code,
        target_delivery_ids=delivery_ids,
    )


def run(args: argparse.Namespace) -> int:
    if args.job_id < 1:
        raise PreparationError("TARGET_JOB_INVALID", "--job-id must be positive.")
    settings = get_settings()
    try:
        database_url = operator_gate_rehearsal_database_url(environment={
            "DATABASE_URL": settings.database_url,
            "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
            "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
            "OPERATOR_GATE_REHEARSAL_DATABASE_URL": settings.operator_gate_rehearsal_database_url,
        })
        factory = verified_session_factory(
            database_url=database_url,
            expected_database_name=OPERATOR_GATE_REHEARSAL_DATABASE_NAME,
        )
    except RehearsalDatabaseSafetyError as exc:
        raise PreparationError("DB_PRECHECK_FAILED", str(exc)) from exc
    try:
        _verify_revision(factory)
        result = prepare_job_to_operator_gate(
            factory, job_id=args.job_id, timeout_seconds=args.timeout_seconds
        )
        print("\nOPERATOR EXECUTION GATE REHEARSAL PREPARATION\n")
        print(f"Database        {OPERATOR_GATE_REHEARSAL_DATABASE_NAME}")
        print(f"Job             {result.job_id} ({result.job_code})")
        print(f"BASE            COMPLETED (step {result.base_step_id})")
        print(f"Target Step     {result.target_step_id} ({result.target_operation_code})")
        print(f"Deliveries      COMPLETED {list(result.target_delivery_ids)}")
        print("Operator Release NULL")
        print("Readiness       OPERATOR_EXECUTION_READY_REQUIRED")
        print("Robot Cell      NOT DISPATCHED for target Step")
        print("PRE_ROOF        NOT REACHED")
        print("RESULT          GUI READY: 조립 시작")
        return 0
    finally:
        factory.kw["bind"].dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except PreparationError as exc:
        print(f"{exc.phase}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
