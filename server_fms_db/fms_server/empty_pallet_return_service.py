"""Explicit, policy-neutral empty-pallet cleanup transport boundary.

This service deliberately has no automatic trigger.  A future pallet-empty
authority or DROP resource manager may call ``execute_empty_pallet_return``
after it has made its own decision.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fms_server.forklift_action_adapter import ForkliftExecutionResult
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.transport_location_resolver import (
    LogicalTransportLocations,
    TransportLocationResolutionError,
    resolve_empty_return_locations,
)
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    MaterialDeliveryStatus,
    JobStatus,
    ProductionJob,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    DropResourceOwnershipError,
    DropResourceService,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.realtime.production_events import get_production_change_callback


class EmptyPalletReturnError(RuntimeError):
    """The requested cleanup move is not safe to start."""


class EmptyPalletReturnNotEligibleError(EmptyPalletReturnError):
    pass


class EmptyPalletReturnDuplicateError(EmptyPalletReturnError):
    pass


@dataclass(frozen=True)
class EmptyPalletReturnClaim:
    job_id: int
    delivery_id: int
    request_id: str
    attempt_id: int
    locations: LogicalTransportLocations


class EmptyPalletReturnService:
    """Create and execute one distinct DROP-to-rack cleanup attempt.

    It never changes ``JobMaterialDelivery.status``: the material supply was
    completed before a pallet can be returned.
    """

    _ACTIVE_ATTEMPT_STATUSES = (
        ExecutionAttemptStatus.CREATED,
        ExecutionAttemptStatus.DISPATCHING,
        ExecutionAttemptStatus.ACCEPTED,
        ExecutionAttemptStatus.UNKNOWN,
    )

    def __init__(
        self,
        session: Session,
        *,
        forklift_execution_coordinator: ForkliftExecutionCoordinator,
    ) -> None:
        self._session = session
        self._forklift_execution_coordinator = forklift_execution_coordinator
        self._drop_resource_service = DropResourceService(session)

    def claim_empty_pallet_return(
        self,
        *,
        job_delivery_id: int,
        production_job_id: int | None = None,
        require_consumption_evidence: bool = False,
        disallow_existing_return_history: bool = False,
    ) -> EmptyPalletReturnClaim:
        """Persist a new return Attempt and commit it before Action I/O.

        Mutable lifecycle rows are always locked in canonical order:
        ProductionJob, JobMaterialDelivery, JobMaterialDeliveryItem, JobStep.
        A successful cleanup is intentionally final; a failed or canceled
        cleanup remains visible for explicit recovery policy.
        """
        try:
            # Resolve the parent without a row lock solely to establish the
            # canonical first lock. Revalidate the delivery after locking Job.
            parent_job_id = self._session.scalar(
                select(JobMaterialDelivery.production_job_id).where(
                    JobMaterialDelivery.job_delivery_id == job_delivery_id
                )
            )
            if parent_job_id is None:
                raise EmptyPalletReturnNotEligibleError(
                    f"Job material delivery not found: job_delivery_id={job_delivery_id}."
                )
            if production_job_id is not None and parent_job_id != production_job_id:
                raise EmptyPalletReturnNotEligibleError(
                    "Job material delivery not found for production job."
                )
            job = self._session.scalar(
                select(ProductionJob)
                .where(ProductionJob.job_id == parent_job_id)
                .with_for_update()
            )
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise EmptyPalletReturnNotEligibleError(
                    "Empty-pallet return is not allowed for a terminal production job."
                )
            if require_consumption_evidence:
                # Automatic/operator paths lock Delivery, Items, then JobSteps
                # only after acquiring the parent Job lock above.
                from fms_server.empty_pallet_return_eligibility import EmptyPalletReturnEligibilityService

                delivery = EmptyPalletReturnEligibilityService(self._session).assert_eligible(
                    job_delivery_id=job_delivery_id,
                    production_job_id=job.job_id,
                    lock_rows=True,
                )
            else:
                delivery = self._session.scalar(
                    select(JobMaterialDelivery)
                    .where(
                        JobMaterialDelivery.job_delivery_id == job_delivery_id,
                        JobMaterialDelivery.production_job_id == job.job_id,
                    )
                    .with_for_update()
                )
                if delivery is None:
                    raise EmptyPalletReturnNotEligibleError(
                        "Job material delivery not found for production job."
                    )
                self._validate_delivery(delivery)
            # Hold the PostgreSQL transaction-scoped DROP guard only through
            # owner verification and durable Attempt creation, never Action I/O.
            self._drop_resource_service.acquire_drop_transaction_guard()
            try:
                locations = resolve_empty_return_locations(delivery)
            except TransportLocationResolutionError as exc:
                raise EmptyPalletReturnNotEligibleError(str(exc)) from exc

            existing_attempts = list(
                self._session.scalars(
                    select(ExecutionAttempt)
                    .where(
                        ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                        ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                        ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
                    )
                    .with_for_update()
                )
            )
            if disallow_existing_return_history and existing_attempts:
                raise EmptyPalletReturnDuplicateError(
                    f"An empty-pallet return history already exists for delivery_id={job_delivery_id}."
                )
            if any(attempt.status in self._ACTIVE_ATTEMPT_STATUSES for attempt in existing_attempts):
                raise EmptyPalletReturnDuplicateError(
                    f"An active empty-pallet return already exists for delivery_id={job_delivery_id}."
                )
            if any(attempt.status is ExecutionAttemptStatus.SUCCEEDED for attempt in existing_attempts):
                raise EmptyPalletReturnDuplicateError(
                    f"The empty-pallet return already succeeded for delivery_id={job_delivery_id}."
                )
            try:
                self._drop_resource_service.assert_delivery_owns_drop(
                    job_delivery_id=delivery.job_delivery_id
                )
            except DropResourceOwnershipError as exc:
                raise EmptyPalletReturnNotEligibleError(
                    f"Only the current DROP owner may start empty return: {exc}"
                ) from exc

            request_id = str(uuid.uuid4())
            payload = {
                "req_id": request_id,
                "job_id": delivery.production_job_id,
                "delivery_id": delivery.job_delivery_id,
                "pickup_code": locations.pickup_code,
                "dropoff_code": locations.dropoff_code,
            }
            attempt = ExecutionAttempt(
                req_id=request_id,
                executor_type=ExecutorType.FORKLIFT,
                command_type=EMPTY_RETURN_COMMAND_TYPE,
                job_id=delivery.production_job_id,
                job_delivery_id=delivery.job_delivery_id,
                attempt_no=(
                    self._session.scalar(
                        select(func.max(ExecutionAttempt.attempt_no)).where(
                            ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                            ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
                        )
                    )
                    or 0
                )
                + 1,
                status=ExecutionAttemptStatus.CREATED,
                request_payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                created_at=self._utcnow(),
            )
            self._session.add(attempt)
            self._session.flush()
            self._session.commit()
            return EmptyPalletReturnClaim(
                job_id=delivery.production_job_id,
                delivery_id=delivery.job_delivery_id,
                request_id=request_id,
                attempt_id=attempt.attempt_id,
                locations=locations,
            )
        except Exception:
            self._session.rollback()
            raise

    def execute_empty_pallet_return(
        self,
        *,
        job_delivery_id: int,
        production_job_id: int | None = None,
        require_consumption_evidence: bool = False,
        disallow_existing_return_history: bool = False,
    ) -> ForkliftExecutionResult:
        """Explicitly dispatch one already-committed cleanup Attempt.

        No Worker tick, callback, GUI action, or job/step state invokes this
        method in Phase C.
        """
        claim = self.claim_empty_pallet_return(
            job_delivery_id=job_delivery_id,
            production_job_id=production_job_id,
            require_consumption_evidence=require_consumption_evidence,
            disallow_existing_return_history=disallow_existing_return_history,
        )
        try:
            result = self._forklift_execution_coordinator.execute_transport(
                job_id=claim.job_id,
                delivery_id=claim.delivery_id,
                pickup_code=claim.locations.pickup_code,
                dropoff_code=claim.locations.dropoff_code,
                req_id=claim.request_id,
                attempt_preclaimed=True,
                command_type=EMPTY_RETURN_COMMAND_TYPE,
            )
            self._session.commit()
            if result.status.value == "SUCCEEDED":
                ProductionOrchestrationService(self._session).try_enter_pre_roof_ready_after_inner_return(claim.job_id)
                self._notify_production_after_commit(
                    job_id=claim.job_id,
                    reason="empty_pallet_return_completed",
                )
            return result
        except Exception as exc:
            self._mark_dispatch_exception(request_id=claim.request_id, detail=str(exc))
            raise

    @staticmethod
    def _notify_production_after_commit(*, job_id: int, reason: str) -> None:
        callback = get_production_change_callback()
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:
            # Realtime is best-effort and must never change a committed return.
            import logging
            logging.getLogger(__name__).exception(
                "Empty-pallet return production notification failed job_id=%s", job_id
            )

    @staticmethod
    def _validate_delivery(delivery: JobMaterialDelivery) -> None:
        if delivery.supply_mode is not SupplyMode.TRANSPORTED:
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return is only applicable to TRANSPORTED Deliveries."
            )
        if delivery.status is not MaterialDeliveryStatus.COMPLETED:
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return requires an already COMPLETED material Delivery."
            )

    def _mark_dispatch_exception(self, *, request_id: str, detail: str) -> None:
        try:
            attempt = self._session.scalar(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.req_id == request_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if attempt is not None and attempt.status not in (
                ExecutionAttemptStatus.SUCCEEDED,
                ExecutionAttemptStatus.FAILED,
                ExecutionAttemptStatus.CANCELED,
            ):
                ExecutionAttemptService(self._session).apply_result(
                    request_id,
                    ExecutionAttemptStatus.FAILED,
                    result_payload={"status": "FAILED", "error_code": "DISPATCH_ERROR"},
                    error_code="DISPATCH_ERROR",
                    detail=detail,
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
