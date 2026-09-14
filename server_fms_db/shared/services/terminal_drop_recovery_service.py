"""Bounded benchmark maintenance authority for a stranded terminal-job DROP pallet."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.transport_location_resolver import (
    TransportLocationResolutionError,
    resolve_empty_return_locations,
)
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    ProductionJob,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    DropResourceService,
    DropResourceState,
)
from shared.realtime.production_events import get_production_change_callback
from shared.services.execution_attempt_service import ExecutionAttemptService


class TerminalDropRecoveryError(RuntimeError):
    """The durable state is not the narrowly approved stranded-pallet shape."""


class TerminalDropAlreadyRecovered(TerminalDropRecoveryError):
    """A successful durable empty-return Attempt already exists."""


_TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED})
_ACTIVE_ATTEMPT_STATUSES = frozenset({
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
})
_RECOVERY_MARKER = "BENCHMARK_TERMINAL_DROP_RECOVERY"


@dataclass(frozen=True, slots=True)
class TerminalDropRecoveryAssessment:
    job_id: int
    job_status: JobStatus
    delivery_id: int
    supply_group_code: str | None
    forward_attempt_id: int
    forward_status: ExecutionAttemptStatus
    drop_state: DropResourceState
    drop_owner_delivery_id: int | None
    pickup_code: str
    dropoff_code: str


@dataclass(frozen=True, slots=True)
class TerminalDropRecoveryResult:
    assessment: TerminalDropRecoveryAssessment
    attempt_id: int
    req_id: str


class TerminalDropRecoveryService:
    """Create only missing return evidence for a terminal benchmark Job.

    This deliberately does not relax normal terminal-job empty-return policy.
    The caller must separately enforce the benchmark-only database boundary.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._drop = DropResourceService(session)

    def assess(self, *, job_id: int, delivery_id: int, lock_rows: bool = False) -> TerminalDropRecoveryAssessment:
        job_query = select(ProductionJob).where(ProductionJob.job_id == job_id)
        delivery_query = select(JobMaterialDelivery).where(
            JobMaterialDelivery.job_delivery_id == delivery_id
        )
        if lock_rows:
            job_query = job_query.with_for_update()
            delivery_query = delivery_query.with_for_update()
        job = self._session.scalar(job_query)
        delivery = self._session.scalar(delivery_query)
        if job is None:
            raise TerminalDropRecoveryError(f"ProductionJob not found: job_id={job_id}.")
        if delivery is None or delivery.production_job_id != job_id:
            raise TerminalDropRecoveryError("Delivery does not belong to the requested ProductionJob.")
        if job.status not in _TERMINAL_JOB_STATUSES:
            raise TerminalDropRecoveryError("Terminal DROP recovery requires a terminal ProductionJob.")
        if delivery.supply_mode is not SupplyMode.TRANSPORTED:
            raise TerminalDropRecoveryError("Terminal DROP recovery requires a TRANSPORTED Delivery.")
        if delivery.status is not MaterialDeliveryStatus.COMPLETED:
            raise TerminalDropRecoveryError("Terminal DROP recovery requires a COMPLETED Delivery.")

        attempts_query = select(ExecutionAttempt).where(
            ExecutionAttempt.job_delivery_id == delivery_id,
            ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
            ExecutionAttempt.command_type.in_((MATERIAL_TRANSPORT_COMMAND_TYPE, EMPTY_RETURN_COMMAND_TYPE)),
        )
        if lock_rows:
            attempts_query = attempts_query.with_for_update()
        attempts = list(self._session.scalars(attempts_query))
        successful_returns = [
            attempt for attempt in attempts
            if attempt.command_type == EMPTY_RETURN_COMMAND_TYPE
            and attempt.status is ExecutionAttemptStatus.SUCCEEDED
        ]
        if successful_returns:
            raise TerminalDropAlreadyRecovered(
                f"Empty return already succeeded: attempt_id={successful_returns[-1].attempt_id}."
            )
        active_returns = [
            attempt for attempt in attempts
            if attempt.command_type == EMPTY_RETURN_COMMAND_TYPE
            and attempt.status in _ACTIVE_ATTEMPT_STATUSES
        ]
        if active_returns:
            raise TerminalDropRecoveryError(
                f"An empty-return Attempt is already active: attempt_id={active_returns[-1].attempt_id}."
            )
        forward_attempts = [
            attempt for attempt in attempts
            if attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE
            and attempt.status is ExecutionAttemptStatus.SUCCEEDED
        ]
        if not forward_attempts:
            raise TerminalDropRecoveryError("No successful forward EXECUTE_TRANSPORT Attempt exists.")

        active_movements = list(self._session.scalars(
            select(ExecutionAttempt.attempt_id).where(
                ExecutionAttempt.status.in_(_ACTIVE_ATTEMPT_STATUSES)
            )
        ))
        if active_movements:
            raise TerminalDropRecoveryError(
                f"Recovery is unsafe while active execution Attempts exist: {active_movements}."
            )

        snapshot = self._drop.get_drop_state()
        if (
            snapshot.state is not DropResourceState.OCCUPIED
            or snapshot.owner_delivery_id != delivery_id
        ):
            raise TerminalDropRecoveryError(
                "DROP is not occupied by the requested Delivery: "
                f"state={snapshot.state.value}, owner_delivery_id={snapshot.owner_delivery_id}."
            )
        try:
            locations = resolve_empty_return_locations(delivery)
        except TransportLocationResolutionError as exc:
            raise TerminalDropRecoveryError(str(exc)) from exc
        forward = max(forward_attempts, key=lambda attempt: attempt.attempt_no)
        return TerminalDropRecoveryAssessment(
            job_id=job_id,
            job_status=job.status,
            delivery_id=delivery_id,
            supply_group_code=delivery.supply_group_code,
            forward_attempt_id=forward.attempt_id,
            forward_status=forward.status,
            drop_state=snapshot.state,
            drop_owner_delivery_id=snapshot.owner_delivery_id,
            pickup_code=locations.pickup_code,
            dropoff_code=locations.dropoff_code,
        )

    def recover(self, *, job_id: int, delivery_id: int) -> TerminalDropRecoveryResult:
        """Atomically add a successful empty-return Attempt, with no adapter I/O."""
        try:
            self._drop.acquire_drop_transaction_guard()
            assessment = self.assess(job_id=job_id, delivery_id=delivery_id, lock_rows=True)
            req_id = str(uuid.uuid4())
            payload = {
                "req_id": req_id,
                "job_id": assessment.job_id,
                "delivery_id": assessment.delivery_id,
                "pickup_code": assessment.pickup_code,
                "dropoff_code": assessment.dropoff_code,
            }
            attempt = ExecutionAttemptService(self._session).record_synthetic_success_in_current_transaction(
                executor_type=ExecutorType.FORKLIFT,
                command_type=EMPTY_RETURN_COMMAND_TYPE,
                request_payload=payload,
                result_payload={"status": "SUCCEEDED", "recovery": _RECOVERY_MARKER},
                job_id=assessment.job_id,
                job_delivery_id=assessment.delivery_id,
                req_id=req_id,
                detail=f"[{_RECOVERY_MARKER}] approved terminal-job DROP recovery",
            )
            snapshot = self._drop.get_drop_state()
            if snapshot.state is not DropResourceState.FREE or snapshot.owner_delivery_id is not None:
                raise TerminalDropRecoveryError(
                    "Synthetic empty return did not durably release DROP before commit."
                )
            self._session.commit()
            self._notify_production_after_commit(
                job_id=assessment.job_id,
                reason="terminal_drop_recovery_completed",
            )
            return TerminalDropRecoveryResult(
                assessment=assessment, attempt_id=attempt.attempt_id, req_id=req_id
            )
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _notify_production_after_commit(*, job_id: int, reason: str) -> None:
        """Realtime is observability only; never invalidate committed recovery."""
        callback = get_production_change_callback()
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Terminal DROP recovery production notification failed job_id=%s", job_id
            )
