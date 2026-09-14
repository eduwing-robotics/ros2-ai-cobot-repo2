#!/usr/bin/env python3
"""Prepare one rehearsal HOUSE_B Job through normal Fake Server/FMS authority.

The command is deliberately bounded at PRE_ROOF_READY: it never starts a
PRE_ROOF inspection, materializes Roof, or calls the trusted PRE_ROOF PASS seam.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.incoming_material_qa_udp_transport import IncomingQAUdpRuntime, IncomingQAUdpRuntimeConfig
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from shared.config import MaterialPrefetchMode
from scripts.fake_vision_incoming_qa_v02 import (
    FakeVisionIncomingQAConfig,
    FakeVisionIncomingQASimulator,
    FakeVisionScenario,
)
from scripts.rehearsal_database import RehearsalDatabaseSafetyError, rehearsal_database_url, verified_session_factory
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    IncomingQATransaction,
    IncomingQATransactionStatus,
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionStatus,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionJob,
    StepStatus,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.physical_ready_service import PhysicalReadyService
from shared.services.operator_execution_ready_service import (
    OperatorExecutionReadyService,
    operator_execution_ready_required,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.test_override_transport_service import TestOverrideTransportService

EXPECTED_REVISION = "20260907_02"
ACTIVE_JOB_STATUSES = frozenset({
    JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
    JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY,
})


class PreparationError(RuntimeError):
    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(detail)
        self.phase = phase


@dataclass(frozen=True)
class PreparationResult:
    job_id: int
    job_code: str
    dispatched_steps: tuple[tuple[str, str], ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser.parse_args(argv)


def _verify_revision(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != EXPECTED_REVISION:
        raise PreparationError(
            "ALEMBIC_REVISION_MISMATCH",
            f"Expected {EXPECTED_REVISION}, got {revision!r}; no migration was run.",
        )


def _verify_target_job(factory: sessionmaker[Session], *, job_id: int) -> ProductionJob:
    with factory() as session:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise PreparationError("TARGET_JOB_INVALID", f"Job {job_id} was not found.")
        if job.status is not JobStatus.REQUESTED:
            raise PreparationError(
                "TARGET_JOB_INVALID",
                f"Job {job_id} must be REQUESTED before preparation, got {job.status.value}.",
            )
        others = list(session.scalars(
            select(ProductionJob).where(
                ProductionJob.status.in_(ACTIVE_JOB_STATUSES),
                ProductionJob.job_id != job_id,
            )
        ))
        if others:
            labels = ", ".join(f"{row.job_id}:{row.job_code}:{row.status.value}" for row in others[:5])
            raise PreparationError(
                "EXTRA_ACTIVE_JOB",
                "FMS worker scans globally; refusing while other active rehearsal jobs exist: " + labels,
            )
        inspection_count = session.scalar(
            select(func.count()).select_from(ProductionInspection).where(
                ProductionInspection.production_job_id == job_id
            )
        )
        if inspection_count:
            raise PreparationError(
                "TARGET_JOB_INVALID", "Target already has a ProductionInspection; preparation must start before PRE_ROOF."
            )
        session.expunge(job)
        return job


def _make_worker(factory: sessionmaker[Session], *, cell: FakeCellActionTransport) -> FmsWorker:
    cell_adapter = RobotCellActionAdapter(cell)
    forklift_adapter = ForkliftActionAdapter(FakeForkliftActionTransport())

    def fms_factory(session: Session) -> FmsExecutionCoordinator:
        return FmsExecutionCoordinator(
            session,
            orchestration_service=ProductionOrchestrationService(session),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
        )

    def forklift_factory(session: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(
            session,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
        )

    def feed_factory(session: Session) -> MaterialFeedExecutionCoordinator:
        return MaterialFeedExecutionCoordinator(session, robot_cell_adapter=cell_adapter)

    return FmsWorker(
        session_factory=factory,
        fms_execution_coordinator_factory=fms_factory,
        forklift_execution_coordinator_factory=forklift_factory,
        material_feed_execution_coordinator_factory=feed_factory,
        auto_empty_pallet_return_enabled=False,
        material_prefetch_mode=MaterialPrefetchMode.DISABLED,
    )


async def _wait_for_transaction(
    factory: sessionmaker[Session], transaction_id: int, *, deadline: float
) -> None:
    while time.monotonic() < deadline:
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            if transaction is not None and transaction.status is IncomingQATransactionStatus.COMPLETED:
                active_items = session.scalar(
                    select(func.count()).select_from(MaterialInspection).where(
                        MaterialInspection.incoming_qa_transaction_id == transaction_id,
                        MaterialInspection.status.in_((
                            MaterialInspectionStatus.REQUESTED,
                            MaterialInspectionStatus.RUNNING,
                        )),
                    )
                )
                if active_items == 0:
                    return
            if transaction is not None and transaction.status in {
                IncomingQATransactionStatus.ERROR, IncomingQATransactionStatus.REJECTED,
            }:
                raise PreparationError("INCOMING_QA_NOT_RELEASED", f"Incoming QA transaction ended {transaction.status.value}.")
        await asyncio.sleep(0.02)
    raise PreparationError("INCOMING_QA_NOT_RELEASED", "Timed out waiting for Fake Incoming QA completion.")


def _free_loopback_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


async def _prepare_incoming_qa(factory: sessionmaker[Session], *, job_id: int, timeout: float) -> None:
    result_port = _free_loopback_udp_port()
    fake = FakeVisionIncomingQASimulator(
        FakeVisionIncomingQAConfig(
            host="127.0.0.1", request_port=0,
            server_result_host="127.0.0.1", server_result_port=result_port,
            scenario=FakeVisionScenario.ALL_PASS,
            result_delay_seconds=0.15,
        )
    )
    await fake.start()
    assert fake.bound_port is not None
    runtime = IncomingQAUdpRuntime(
        session_factory=factory,
        config=IncomingQAUdpRuntimeConfig(
            vision_host="127.0.0.1", vision_port=fake.bound_port,
            result_host="127.0.0.1", result_port=result_port,
            ack_timeout_seconds=0.2, max_retries=1,
        ),
    )
    await runtime.start()
    deadline = time.monotonic() + timeout
    try:
        with factory() as session:
            plan = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
            if plan.send_transaction_id is None:
                raise PreparationError("INCOMING_QA_NOT_RELEASED", "Initial Incoming QA did not create a dispatchable transaction.")
            first = plan.send_transaction_id
        await runtime.send_transaction(first)
        await _wait_for_transaction(factory, first, deadline=deadline)
        with factory() as session:
            followup = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
            if followup.send_transaction_id is None:
                raise PreparationError("INCOMING_QA_NOT_RELEASED", "HOUSE_B follow-up Incoming QA was not planned.")
            second = followup.send_transaction_id
        await runtime.send_transaction(second)
        await _wait_for_transaction(factory, second, deadline=deadline)
        with factory() as session:
            readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
            if not readiness.ready:
                raise PreparationError("INCOMING_QA_NOT_RELEASED", "Latest-effective Incoming QA did not reach RELEASE.")
    finally:
        await runtime.close()
        await fake.close()


def _prepare_material_readiness(factory: sessionmaker[Session], *, job_id: int) -> None:
    with factory() as session:
        deliveries = {
            delivery.supply_group_code: delivery.job_delivery_id
            for delivery in session.scalars(
                select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job_id)
            )
        }
    required = {"BASE", "INNER_WALL", "OUTER_WALLS", "ROOF"}
    if set(deliveries) != required:
        raise PreparationError("MATERIAL_READINESS_BLOCKED", f"Unexpected delivery groups: {sorted(deliveries)}")
    # Existing operator-authority services: no Delivery.status assignments.
    with factory() as session:
        ManualPrestageService(session).confirm_manual_prestage_ready(
            job_id=job_id, job_delivery_id=deliveries["BASE"], request_id=f"rehearsal-{job_id}-base-manual"
        )
    for group in ("INNER_WALL", "OUTER_WALLS"):
        with factory() as session:
            PhysicalReadyService(session).confirm_physical_ready(
                job_delivery_id=deliveries[group], request_id=f"rehearsal-{job_id}-{group.lower()}-physical"
            )
        with factory() as session:
            delivery_service = MaterialDeliveryService(session)
            delivery_service.start_delivery(deliveries[group])
            delivery_service.complete_delivery(deliveries[group])


def _initial_step_count(factory: sessionmaker[Session], *, job_id: int) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count()).select_from(JobStep).where(JobStep.job_id == job_id)
        ) or 0


def _current_step(factory: sessionmaker[Session], *, job_id: int) -> JobStep | None:
    with factory() as session:
        step = ProductionOrchestrationService(session).get_next_step(job_id)
        if step is not None:
            session.expunge(step)
        return step


def _verify_step_completed(factory: sessionmaker[Session], *, job_id: int, job_step_id: int) -> tuple[str, str]:
    with factory() as session:
        step = session.get(JobStep, job_step_id)
        if step is None or step.status is not StepStatus.COMPLETED:
            raise PreparationError("STEP_COMPLETION_TIMEOUT", f"JobStep {job_step_id} was not completed by Fake Cell authority.")
        attempt = session.scalar(select(ExecutionAttempt).where(
            ExecutionAttempt.job_step_id == job_step_id,
            ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
        ))
        if attempt is None or attempt.status is not ExecutionAttemptStatus.SUCCEEDED:
            raise PreparationError("STEP_COMPLETION_TIMEOUT", f"JobStep {job_step_id} lacks a SUCCEEDED Robot Cell attempt.")
        return step.operation_code, step.part_code or ""


def prepare_job_to_pre_roof_ready(
    factory: sessionmaker[Session], *, job_id: int, timeout_seconds: float = 20.0,
    emit: Callable[[str], None] = print,
) -> PreparationResult:
    if timeout_seconds <= 0:
        raise PreparationError("PRE_ROOF_READY_TIMEOUT", "timeout_seconds must be positive.")
    job = _verify_target_job(factory, job_id=job_id)
    emit("Incoming QA     DISPATCHED (loopback Fake Vision v0.2)")
    asyncio.run(_prepare_incoming_qa(factory, job_id=job_id, timeout=timeout_seconds))
    emit("Incoming QA     RELEASED")
    _prepare_material_readiness(factory, job_id=job_id)
    emit("Material         BASE manual-prestaged; INNER_WALL/OUTER_WALLS physical-ready and delivered")
    step_count = _initial_step_count(factory, job_id=job_id)
    if step_count < 1:
        raise PreparationError("UNEXPECTED_STAGE", "Target has no initial executable JobStep snapshot.")
    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(step_count)
    ])
    worker = _make_worker(factory, cell=cell)
    completed: list[tuple[str, str]] = []
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with factory() as session:
            current_job = session.get(ProductionJob, job_id)
            if current_job is not None and current_job.status is JobStatus.PRE_ROOF_READY:
                break
        step = _current_step(factory, job_id=job_id)
        if step is None:
            # All assembly steps may be complete while the new durable inner
            # pallet-return prerequisite still owns DROP.  The rehearsal seam
            # records the canonical no-I/O return success, then the normal
            # orchestration service enters PRE_ROOF_READY.
            with factory() as session:
                inner = session.scalar(select(JobMaterialDelivery).where(
                    JobMaterialDelivery.production_job_id == job_id,
                    JobMaterialDelivery.supply_group_code == "INNER_WALL",
                ))
                if inner is None:
                    raise PreparationError("UNEXPECTED_STAGE", "INNER_WALL delivery is missing before PRE_ROOF_READY.")
                # The legacy rehearsal fixture marks delivery complete without
                # a transport Attempt; establish the same durable fake-arrival
                # evidence before exercising the canonical return seam.
                forward = session.scalar(select(ExecutionAttempt).where(
                    ExecutionAttempt.job_delivery_id == inner.job_delivery_id,
                    ExecutionAttempt.command_type == "EXECUTE_TRANSPORT",
                ))
                if forward is None:
                    session.add(ExecutionAttempt(
                        req_id=f"rehearsal-inner-arrival-{job_id}", executor_type=ExecutorType.FORKLIFT,
                        command_type="EXECUTE_TRANSPORT", job_id=job_id,
                        job_delivery_id=inner.job_delivery_id, attempt_no=1,
                        status=ExecutionAttemptStatus.SUCCEEDED,
                        request_payload_json='{"pickup_code":"RACK2","dropoff_code":"DROP"}',
                    ))
                    session.commit()
                TestOverrideTransportService(session).complete_empty_pallet_return(
                    production_job_id=job_id, job_delivery_id=inner.job_delivery_id
                )
            emit("INNER_WALL_RETURN   COMPLETED")
            continue
        # Fake preparation represents the same durable operator safety release
        # as the GUI command. It never changes Step status or calls Cell directly.
        if operator_execution_ready_required(step):
            with factory() as session:
                OperatorExecutionReadyService(session).confirm(
                    job_id=job_id, job_step_id=step.job_step_id
                )
            emit(f"{step.operation_code:<20} OPERATOR_EXECUTION_RELEASED")
        emit(f"{step.operation_code:<20} DISPATCHED")
        if not worker.tick():
            raise PreparationError("FAKE_CELL_DISPATCH_TIMEOUT", f"Worker did not dispatch {step.operation_code}.")
        operation, part_code = _verify_step_completed(factory, job_id=job_id, job_step_id=step.job_step_id)
        completed.append((operation, part_code))
        emit(f"{operation:<20} COMPLETED")
    else:
        raise PreparationError("PRE_ROOF_READY_TIMEOUT", "Timed out waiting for PRE_ROOF_READY.")

    with factory() as session:
        final_job = session.get(ProductionJob, job_id)
        if final_job is None or final_job.status is not JobStatus.PRE_ROOF_READY:
            raise PreparationError("PRE_ROOF_READY_TIMEOUT", "Job did not reach PRE_ROOF_READY.")
        roof_steps = session.scalar(
            select(func.count()).select_from(JobStep).join(
                AssemblyRecipeStage,
                JobStep.source_recipe_stage_id == AssemblyRecipeStage.recipe_stage_id,
            ).where(
                JobStep.job_id == job_id,
                AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
            )
        )
        inspections = list(session.scalars(select(ProductionInspection).where(
            ProductionInspection.production_job_id == job_id
        ).order_by(ProductionInspection.inspection_cycle, ProductionInspection.inspection_id)))
        if roof_steps:
            raise PreparationError("UNEXPECTED_ROOF_MATERIALIZED", "Roof gated JobStep exists before PRE_ROOF rehearsal.")
        # Existing PRE_ROOF_READY authority creates exactly one inert PENDING
        # placeholder.  It is not a started wire inspection: start_pre_roof_
        # inspection transitions this same row to RUNNING in the next rehearsal.
        if len(inspections) != 1 or inspections[0].status is not ProductionInspectionStatus.PENDING:
            raise PreparationError(
                "UNEXPECTED_PRE_ROOF_INSPECTION",
                "PRE_ROOF_READY must have exactly one canonical PENDING placeholder inspection.",
            )
        if final_job.completed_at is not None:
            raise PreparationError("UNEXPECTED_ROOF_MATERIALIZED", "Preparation unexpectedly completed the Job.")
    return PreparationResult(job_id=job_id, job_code=job.job_code, dispatched_steps=tuple(completed))


def run(args: argparse.Namespace) -> int:
    if args.job_id < 1:
        raise PreparationError("TARGET_JOB_INVALID", "--job-id must be positive.")
    settings = get_settings()
    try:
        database_url = rehearsal_database_url(environment={
            "DATABASE_URL": settings.database_url,
            "POSTGRES_TEST_DATABASE_URL": settings.postgres_test_database_url,
            "FACTORY_REHEARSAL_DATABASE_URL": settings.factory_rehearsal_database_url,
        })
        factory = verified_session_factory(database_url=database_url)
    except RehearsalDatabaseSafetyError as exc:
        raise PreparationError("DB_PRECHECK_FAILED", str(exc)) from exc
    try:
        _verify_revision(factory)
        result = prepare_job_to_pre_roof_ready(factory, job_id=args.job_id, timeout_seconds=args.timeout_seconds)
        print("\nPRE_ROOF REHEARSAL JOB PREPARATION\n")
        print("Database        smart_factory_rehearsal")
        print(f"Job             {result.job_id} ({result.job_code})")
        print("Incoming QA     RELEASED")
        for operation, part_code in result.dispatched_steps:
            print(f"{operation:<20} COMPLETED ({part_code})")
        print("Job state       PRE_ROOF_READY")
        print("Roof stage      NOT MATERIALIZED")
        print("Inspection      PENDING PLACEHOLDER (not started)")
        print("RESULT          READY")
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
