"""Phase 2 QA-before-transport eligibility and atomic Delivery claim boundary."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    MaterialDeliveryStatus,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    DROP_CODE,
    DropResourceService,
    DropResourceUnavailableError,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.forklift_movement_guard import has_active_forklift_movement
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.material_inspection_service import MaterialInspectionService


class TransportEligibilityReason(StrEnum):
    LEGACY_PATH = "LEGACY_PATH"
    MANUAL_TRANSPORT_NOT_APPLICABLE = "MANUAL_TRANSPORT_NOT_APPLICABLE"
    QA_NOT_RELEASED = "QA_NOT_RELEASED"
    PRE_PRODUCTION_QA_INCOMPLETE = "PRE_PRODUCTION_QA_INCOMPLETE"
    PHYSICAL_READY_REQUIRED = "PHYSICAL_READY_REQUIRED"
    DELIVERY_NOT_PENDING = "DELIVERY_NOT_PENDING"
    ACTIVE_TRANSPORT_ATTEMPT = "ACTIVE_TRANSPORT_ATTEMPT"
    FORKLIFT_BUSY = "FORKLIFT_BUSY"
    DROP_RESOURCE_OCCUPIED = "DROP_RESOURCE_OCCUPIED"
    POLICY_INVALID = "POLICY_INVALID"
    ELIGIBLE = "ELIGIBLE"


@dataclass(frozen=True)
class TransportEligibilityResult:
    eligible: bool
    reason: TransportEligibilityReason
    job_delivery_id: int
    request_id: str | None = None
    attempt_id: int | None = None


class TransportEligibilityService:
    """Own policy-based pre-transport safety evaluation and durable claim.

    Legacy Delivery rows deliberately receive `LEGACY_PATH` so the Worker can
    preserve their historical dispatch behavior.  This service never changes
    QA evidence, physical-ready evidence, Feed state, or StepReadiness.
    """

    _ACTIVE_ATTEMPT_STATUSES = (
        ExecutionAttemptStatus.CREATED,
        ExecutionAttemptStatus.DISPATCHING,
        ExecutionAttemptStatus.ACCEPTED,
        ExecutionAttemptStatus.UNKNOWN,
    )

    def __init__(self, session: Session) -> None:
        self._session = session
        self._qa_service = MaterialInspectionService()
        self._drop_resource_service = DropResourceService(session)
        self._preproduction_qa = IncomingQAOrchestrationService(session)

    def evaluate_transport_eligibility(self, *, job_delivery_id: int) -> TransportEligibilityResult:
        delivery = self._session.scalar(
            select(JobMaterialDelivery)
            .options(selectinload(JobMaterialDelivery.items))
            .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
        )
        if delivery is None:
            raise ValueError(f"Job material delivery not found: job_delivery_id={job_delivery_id}.")
        return self._evaluate(delivery)

    def claim_transport(
        self,
        *,
        job_delivery_id: int,
        request_payload: dict[str, Any],
        request_id: str | None = None,
    ) -> TransportEligibilityResult:
        """Atomically reserve a new-policy transported Delivery before Action I/O."""
        try:
            # PostgreSQL transaction-scoped global serialization is acquired
            # before inspecting ownership and creating the material Attempt.
            self._drop_resource_service.acquire_drop_transaction_guard()
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .options(selectinload(JobMaterialDelivery.items))
                .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
                .with_for_update()
            )
            if delivery is None:
                raise ValueError(f"Job material delivery not found: job_delivery_id={job_delivery_id}.")
            evaluation = self._evaluate(delivery)
            if not evaluation.eligible:
                self._session.commit()
                return evaluation

            dispatch_request_id = self._normalized_request_id(request_id)
            attempt = self._create_transport_attempt(
                delivery=delivery,
                request_id=dispatch_request_id,
                request_payload=request_payload,
            )
            delivery.status = MaterialDeliveryStatus.IN_PROGRESS
            delivery.started_at = self._utcnow()
            delivery.failed_at = None
            delivery.failure_reason = None
            self._session.flush()
            self._session.commit()
            return TransportEligibilityResult(
                eligible=True,
                reason=TransportEligibilityReason.ELIGIBLE,
                job_delivery_id=delivery.job_delivery_id,
                request_id=attempt.req_id,
                attempt_id=attempt.attempt_id,
            )
        except Exception:
            self._session.rollback()
            raise

    def fail_claimed_transport(
        self,
        *,
        job_delivery_id: int,
        request_id: str,
        reason: str,
    ) -> None:
        """Persist a visible failed Delivery/attempt when dispatch cannot start."""
        try:
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
                .with_for_update()
            )
            if delivery is None:
                raise ValueError(f"Job material delivery not found: job_delivery_id={job_delivery_id}.")
            if delivery.status is MaterialDeliveryStatus.IN_PROGRESS:
                delivery.status = MaterialDeliveryStatus.FAILED
                delivery.failed_at = self._utcnow()
                delivery.failure_reason = reason
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
                    detail=reason,
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _evaluate(self, delivery: JobMaterialDelivery) -> TransportEligibilityResult:
        if self._is_legacy(delivery):
            return self._result(delivery, TransportEligibilityReason.LEGACY_PATH)
        if not self._has_valid_new_policy(delivery):
            return self._result(delivery, TransportEligibilityReason.POLICY_INVALID)
        if delivery.supply_mode is SupplyMode.MANUAL:
            return self._result(delivery, TransportEligibilityReason.MANUAL_TRANSPORT_NOT_APPLICABLE)
        if not self._preproduction_qa.is_preproduction_ready(
            job_id=delivery.production_job_id
        ):
            return self._result(delivery, TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE)
        if delivery.status is not MaterialDeliveryStatus.PENDING:
            return self._result(delivery, TransportEligibilityReason.DELIVERY_NOT_PENDING)
        if delivery.physical_ready_at is None:
            return self._result(delivery, TransportEligibilityReason.PHYSICAL_READY_REQUIRED)
        if self._has_active_transport_attempt(delivery.job_delivery_id):
            return self._result(delivery, TransportEligibilityReason.ACTIVE_TRANSPORT_ATTEMPT)
        if not self.are_all_delivery_items_released(delivery):
            return self._result(delivery, TransportEligibilityReason.QA_NOT_RELEASED)
        if self._uses_shared_drop(delivery):
            try:
                self._drop_resource_service.assert_drop_available_for_material()
            except DropResourceUnavailableError:
                return self._result(delivery, TransportEligibilityReason.DROP_RESOURCE_OCCUPIED)
        if has_active_forklift_movement(self._session):
            return self._result(delivery, TransportEligibilityReason.FORKLIFT_BUSY)
        return TransportEligibilityResult(
            eligible=True,
            reason=TransportEligibilityReason.ELIGIBLE,
            job_delivery_id=delivery.job_delivery_id,
        )

    def are_all_delivery_items_released(self, delivery: JobMaterialDelivery) -> bool:
        """Return true only when every persisted DeliveryItem has authoritative QA release."""
        return bool(delivery.items) and all(
            self._qa_service.is_delivery_item_released(self._session, item.delivery_item_id)
            for item in delivery.items
        )

    def _has_active_transport_attempt(self, job_delivery_id: int) -> bool:
        return self._session.scalar(
            select(ExecutionAttempt.attempt_id).where(
                ExecutionAttempt.job_delivery_id == job_delivery_id,
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type == "EXECUTE_TRANSPORT",
                ExecutionAttempt.status.in_(self._ACTIVE_ATTEMPT_STATUSES),
            )
        ) is not None

    @staticmethod
    def _uses_shared_drop(delivery: JobMaterialDelivery) -> bool:
        return (
            delivery.supply_mode is SupplyMode.TRANSPORTED
            and isinstance(delivery.supply_destination_code, str)
            and delivery.supply_destination_code.strip() == DROP_CODE
        )

    @staticmethod
    def _is_legacy(delivery: JobMaterialDelivery) -> bool:
        return (
            delivery.supply_mode is None
            and delivery.supply_group_code is None
            and delivery.supply_destination_code is None
        )

    @staticmethod
    def _has_valid_new_policy(delivery: JobMaterialDelivery) -> bool:
        return (
            isinstance(delivery.supply_mode, SupplyMode)
            and isinstance(delivery.supply_group_code, str)
            and bool(delivery.supply_group_code.strip())
        )

    @staticmethod
    def _result(delivery: JobMaterialDelivery, reason: TransportEligibilityReason) -> TransportEligibilityResult:
        return TransportEligibilityResult(
            eligible=False,
            reason=reason,
            job_delivery_id=delivery.job_delivery_id,
        )

    def _create_transport_attempt(
        self,
        *,
        delivery: JobMaterialDelivery,
        request_id: str,
        request_payload: dict[str, Any],
    ) -> ExecutionAttempt:
        canonical_payload = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        max_attempt = self._session.scalar(
            select(func.max(ExecutionAttempt.attempt_no)).where(
                ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                ExecutionAttempt.command_type == "EXECUTE_TRANSPORT",
            )
        ) or 0
        attempt = ExecutionAttempt(
            req_id=request_id,
            executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT",
            job_id=delivery.production_job_id,
            job_delivery_id=delivery.job_delivery_id,
            attempt_no=max_attempt + 1,
            status=ExecutionAttemptStatus.CREATED,
            request_payload_json=canonical_payload,
            created_at=self._utcnow(),
        )
        self._session.add(attempt)
        self._session.flush()
        return attempt

    @staticmethod
    def _normalized_request_id(value: str | None) -> str:
        if value is None:
            return str(uuid.uuid4())
        if not isinstance(value, str) or not (normalized := value.strip()):
            raise ValueError("request_id must be a non-empty string when provided.")
        if len(normalized) > 100:
            raise ValueError("request_id must be 100 characters or fewer.")
        return normalized

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
