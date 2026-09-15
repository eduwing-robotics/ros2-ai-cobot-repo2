"""Atomic, no-I/O transport seams for the explicitly enabled Test Override."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.empty_pallet_return_eligibility import EmptyPalletReturnEligibilityService
from fms_server.empty_pallet_return_service import (
    EmptyPalletReturnDuplicateError,
    EmptyPalletReturnNotEligibleError,
)
from fms_server.transport_location_resolver import (
    TransportLocationResolutionError,
    resolve_empty_return_locations,
    resolve_policy_transport_locations,
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
from shared.realtime.production_events import get_production_change_callback
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    DropResourceOwnershipError,
    DropResourceUnavailableError,
    DropResourceService,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.production_orchestration_service import ProductionOrchestrationService


class TestOverrideTransportError(RuntimeError):
    """The requested synthetic transport is not a safe authoritative transition."""


@dataclass(frozen=True)
class SyntheticDeliveryCompletion:
    delivery: JobMaterialDelivery
    attempt: ExecutionAttempt | None


class TestOverrideTransportService:
    """Create complete durable transport evidence without invoking an adapter.

    The service intentionally owns each database transaction. It is used only
    after TestOverrideService has enforced the benchmark/test capability gate.
    """

    _UNRESOLVED = {
        ExecutionAttemptStatus.CREATED,
        ExecutionAttemptStatus.DISPATCHING,
        ExecutionAttemptStatus.ACCEPTED,
        ExecutionAttemptStatus.UNKNOWN,
    }

    def __init__(self, session: Session, *, post_commit_callback=None) -> None:
        self._session = session
        self._post_commit_callback = post_commit_callback or get_production_change_callback()
        self._drop = DropResourceService(session)

    def complete_pending_delivery(
        self, *, production_job_id: int, job_delivery_id: int
    ) -> SyntheticDeliveryCompletion:
        """Atomically complete one delivery and, if transported, its arrival."""
        try:
            job = self._locked_active_job(production_job_id)
            delivery = self._locked_delivery(job_id=job.job_id, delivery_id=job_delivery_id)
            if delivery.status is not MaterialDeliveryStatus.PENDING:
                raise TestOverrideTransportError("Synthetic delivery requires a PENDING Delivery.")
            self._assert_no_unresolved_delivery_attempt(delivery_id=delivery.job_delivery_id)

            attempt: ExecutionAttempt | None = None
            if delivery.supply_mode is SupplyMode.TRANSPORTED:
                self._drop.acquire_drop_transaction_guard()
                try:
                    self._drop.assert_drop_available_for_material()
                except DropResourceUnavailableError as exc:
                    raise TestOverrideTransportError(str(exc)) from exc
                try:
                    locations = resolve_policy_transport_locations(delivery)
                except TransportLocationResolutionError as exc:
                    raise TestOverrideTransportError(str(exc)) from exc
                request_id = str(uuid.uuid4())
                attempt = ExecutionAttemptService(self._session).record_synthetic_success_in_current_transaction(
                    executor_type=ExecutorType.FORKLIFT,
                    command_type=MATERIAL_TRANSPORT_COMMAND_TYPE,
                    request_payload={
                        "job_id": job.job_id,
                        "delivery_id": delivery.job_delivery_id,
                        "pickup_code": locations.pickup_code,
                        "dropoff_code": locations.dropoff_code,
                    },
                    result_payload={"status": "SUCCEEDED", "source": "TEST_OVERRIDE"},
                    job_id=job.job_id,
                    job_delivery_id=delivery.job_delivery_id,
                    req_id=request_id,
                    detail="Synthetic Test Override transport success; no adapter dispatch.",
                )

            completed = MaterialDeliveryService(
                self._session, post_commit_callback=None
            ).complete_pending_delivery_for_test_override_in_current_transaction(
                delivery.job_delivery_id
            )
            self._session.commit()
            self._notify(job.job_id, "material_delivery_completed")
            return SyntheticDeliveryCompletion(delivery=completed, attempt=attempt)
        except Exception:
            self._session.rollback()
            raise

    def complete_empty_pallet_return(
        self, *, production_job_id: int, job_delivery_id: int
    ) -> ExecutionAttempt:
        """Atomically record a successful DROP-to-rack return without I/O."""
        try:
            job = self._locked_active_job(production_job_id)
            delivery = EmptyPalletReturnEligibilityService(self._session).assert_eligible(
                job_delivery_id=job_delivery_id,
                production_job_id=job.job_id,
                lock_rows=True,
            )
            self._assert_no_unresolved_delivery_attempt(delivery_id=delivery.job_delivery_id)
            self._drop.acquire_drop_transaction_guard()
            try:
                self._drop.assert_delivery_owns_drop(job_delivery_id=delivery.job_delivery_id)
            except DropResourceOwnershipError as exc:
                raise TestOverrideTransportError(str(exc)) from exc
            existing = list(self._session.scalars(
                select(ExecutionAttempt)
                .where(
                    ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                    ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                    ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
                )
                .with_for_update()
            ))
            if any(row.status in self._UNRESOLVED for row in existing):
                raise TestOverrideTransportError("An unresolved empty-pallet return already exists.")
            if any(row.status is ExecutionAttemptStatus.SUCCEEDED for row in existing):
                raise TestOverrideTransportError("The empty-pallet return already succeeded.")
            try:
                locations = resolve_empty_return_locations(delivery)
            except TransportLocationResolutionError as exc:
                raise TestOverrideTransportError(str(exc)) from exc
            request_id = str(uuid.uuid4())
            attempt = ExecutionAttemptService(self._session).record_synthetic_success_in_current_transaction(
                executor_type=ExecutorType.FORKLIFT,
                command_type=EMPTY_RETURN_COMMAND_TYPE,
                request_payload={
                    "req_id": request_id,
                    "job_id": job.job_id,
                    "delivery_id": delivery.job_delivery_id,
                    "pickup_code": locations.pickup_code,
                    "dropoff_code": locations.dropoff_code,
                },
                result_payload={"status": "SUCCEEDED", "source": "TEST_OVERRIDE"},
                job_id=job.job_id,
                job_delivery_id=delivery.job_delivery_id,
                req_id=request_id,
                detail="Synthetic Test Override empty-pallet return success; no adapter dispatch.",
            )
            self._session.commit()
            ProductionOrchestrationService(self._session).try_enter_pre_roof_ready_after_inner_return(job.job_id)
            self._notify(job.job_id, "empty_pallet_return_completed")
            return attempt
        except (EmptyPalletReturnDuplicateError, EmptyPalletReturnNotEligibleError) as exc:
            self._session.rollback()
            raise TestOverrideTransportError(str(exc)) from exc
        except Exception:
            self._session.rollback()
            raise

    def _locked_active_job(self, job_id: int) -> ProductionJob:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
        )
        if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            raise TestOverrideTransportError("Synthetic transport requires a non-terminal ProductionJob.")
        return job

    def _locked_delivery(self, *, job_id: int, delivery_id: int) -> JobMaterialDelivery:
        delivery = self._session.scalar(
            select(JobMaterialDelivery)
            .where(
                JobMaterialDelivery.job_delivery_id == delivery_id,
                JobMaterialDelivery.production_job_id == job_id,
            )
            .with_for_update()
        )
        if delivery is None:
            raise TestOverrideTransportError("Job material delivery not found for production job.")
        return delivery

    def _assert_no_unresolved_delivery_attempt(self, *, delivery_id: int) -> None:
        active = self._session.scalar(
            select(ExecutionAttempt.attempt_id)
            .where(
                ExecutionAttempt.job_delivery_id == delivery_id,
                ExecutionAttempt.status.in_(self._UNRESOLVED),
            )
            .limit(1)
        )
        if active is not None:
            raise TestOverrideTransportError("An unresolved real or synthetic transport Attempt blocks Test Override.")

    def _notify(self, job_id: int, reason: str) -> None:
        if self._post_commit_callback is None:
            return
        try:
            self._post_commit_callback(job_id, reason)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Synthetic Test Override production notification failed job_id=%s", job_id
            )
