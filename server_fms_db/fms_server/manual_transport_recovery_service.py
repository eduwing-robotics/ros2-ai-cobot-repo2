"""Explicit operator-confirmed reconciliation for ambiguous TurtleBot attempts.

This boundary never sends a TurtleBot command.  It records a verified physical
location for one existing Attempt and derives only the corresponding durable FMS
state.  No startup caller or retry loop invokes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    MaterialDeliveryStatus,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    OPERATOR_LOCATION_RECOVERY_KEY,
    DropResourceService,
    DropResourceSnapshot,
    DropResourceState,
    attempt_transport_route,
    operator_location_recovery,
)


class ManualTransportRecoveryError(RuntimeError):
    pass


class ManualTransportRecoveryNotFoundError(ManualTransportRecoveryError):
    pass


class ManualTransportRecoveryConflictError(ManualTransportRecoveryError):
    pass


@dataclass(frozen=True)
class ManualTransportRecoveryResult:
    delivery_id: int
    attempt_id: int
    command_type: str
    confirmed_location_code: str
    attempt_status: ExecutionAttemptStatus
    delivery_status: MaterialDeliveryStatus
    derived_drop_state: DropResourceState
    recovery_applied: bool


class ManualTransportRecoveryService:
    """Reconcile a single persisted forklift Attempt from human location evidence."""

    _RECOVERABLE_COMMAND_TYPES = (
        MATERIAL_TRANSPORT_COMMAND_TYPE,
        EMPTY_RETURN_COMMAND_TYPE,
    )

    def __init__(self, session: Session) -> None:
        self._session = session
        self._drop_resource_service = DropResourceService(session)

    def confirm_location(
        self,
        *,
        production_job_id: int,
        job_delivery_id: int,
        attempt_id: int,
        confirmed_location_code: str,
        operator_note: str | None = None,
    ) -> ManualTransportRecoveryResult:
        location = self._text(confirmed_location_code, field_name="confirmed_location_code")
        note = self._optional_text(operator_note, field_name="operator_note")
        try:
            self._drop_resource_service.acquire_drop_transaction_guard()
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(
                    JobMaterialDelivery.job_delivery_id == job_delivery_id,
                    JobMaterialDelivery.production_job_id == production_job_id,
                )
                .with_for_update()
            )
            if delivery is None:
                raise ManualTransportRecoveryNotFoundError(
                    "Job material delivery not found for production job."
                )
            attempt = self._session.scalar(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.attempt_id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise ManualTransportRecoveryNotFoundError("Execution attempt not found.")
            self._validate_attempt(delivery=delivery, attempt=attempt)
            route = attempt_transport_route(attempt)
            if route is None:
                raise ManualTransportRecoveryConflictError(
                    "Execution attempt has no valid persisted logical pickup/dropoff route."
                )
            pickup_code, dropoff_code = route
            if location not in route:
                raise ManualTransportRecoveryConflictError(
                    "confirmed_location_code must equal this Attempt's persisted pickup_code or dropoff_code."
                )

            previous = operator_location_recovery(attempt)
            if previous is not None:
                previous_location = previous["confirmed_location_code"]
                if previous_location != location:
                    raise ManualTransportRecoveryConflictError(
                        "Attempt already has conflicting operator location recovery evidence."
                    )
                return self._result(
                    delivery=delivery, attempt=attempt, location=location, recovery_applied=False
                )

            if attempt.status is ExecutionAttemptStatus.SUCCEEDED:
                if location != dropoff_code:
                    raise ManualTransportRecoveryConflictError(
                        "A succeeded Attempt cannot be reconciled to its pickup location."
                    )
                if (
                    attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE
                    and delivery.status is MaterialDeliveryStatus.IN_PROGRESS
                ):
                    # A crash may occur after the independently durable terminal
                    # Attempt commit but before the Delivery completion commit.
                    # Operator confirmation of the already-persisted dropoff is
                    # evidence to close only that incomplete Delivery transition;
                    # it never redispatches the original req_id.
                    self._assert_current_recovery_target(delivery=delivery, attempt=attempt)
                    self._record_recovery_evidence(
                        attempt=attempt,
                        confirmed_location_code=location,
                        operator_note=note,
                    )
                    delivery.status = MaterialDeliveryStatus.COMPLETED
                    delivery.completed_at = self._utcnow()
                    delivery.failed_at = None
                    delivery.failure_reason = None
                    self._session.flush()
                    self._session.commit()
                    return self._result(
                        delivery=delivery,
                        attempt=attempt,
                        location=location,
                        recovery_applied=True,
                    )
                # A prior normal terminal result already describes this same
                # endpoint.  Do not rewrite history merely because a stale UI
                # repeats a recovery confirmation.
                return self._result(
                    delivery=delivery, attempt=attempt, location=location, recovery_applied=False
                )
            self._assert_current_recovery_target(delivery=delivery, attempt=attempt)

            completed = location == dropoff_code
            self._record_recovery_evidence(
                attempt=attempt, confirmed_location_code=location, operator_note=note
            )
            attempt.status = (
                ExecutionAttemptStatus.SUCCEEDED if completed else ExecutionAttemptStatus.FAILED
            )
            attempt.completed_at = self._utcnow()
            attempt.error_code = None if completed else "OPERATOR_CONFIRMED_AT_PICKUP"
            attempt.detail = (
                "Operator confirmed pallet at persisted dropoff location."
                if completed
                else "Operator confirmed pallet remained at persisted pickup location."
            )

            if attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE:
                if completed:
                    delivery.status = MaterialDeliveryStatus.COMPLETED
                    delivery.completed_at = self._utcnow()
                    delivery.failed_at = None
                    delivery.failure_reason = None
                else:
                    # The original request remains in Attempt history; only the
                    # Delivery's current eligibility is restored for a future
                    # fresh claim with a fresh req_id.
                    delivery.status = MaterialDeliveryStatus.PENDING
                    delivery.started_at = None
                    delivery.completed_at = None
                    delivery.failed_at = None
                    delivery.failure_reason = None
            elif delivery.status is not MaterialDeliveryStatus.COMPLETED:
                raise ManualTransportRecoveryConflictError(
                    "Empty-pallet return recovery requires a completed material Delivery."
                )

            self._session.flush()
            self._session.commit()
            return self._result(
                delivery=delivery, attempt=attempt, location=location, recovery_applied=True
            )
        except Exception:
            self._session.rollback()
            raise

    def _validate_attempt(
        self, *, delivery: JobMaterialDelivery, attempt: ExecutionAttempt
    ) -> None:
        if attempt.job_delivery_id != delivery.job_delivery_id:
            raise ManualTransportRecoveryConflictError(
                "Execution attempt does not belong to this Job material delivery."
            )
        if attempt.job_id != delivery.production_job_id:
            raise ManualTransportRecoveryConflictError(
                "Execution attempt job identity does not match the Delivery."
            )
        if (
            attempt.executor_type is not ExecutorType.FORKLIFT
            or attempt.command_type not in self._RECOVERABLE_COMMAND_TYPES
        ):
            raise ManualTransportRecoveryConflictError(
                "Only persisted TurtleBot material or empty-return Attempts are recoverable."
            )

    def _assert_current_recovery_target(
        self, *, delivery: JobMaterialDelivery, attempt: ExecutionAttempt
    ) -> None:
        snapshot = self._drop_resource_service.get_drop_state()
        if snapshot.owner_delivery_id != delivery.job_delivery_id:
            raise ManualTransportRecoveryConflictError(
                "Recovery Attempt is stale or this Delivery is not the current DROP owner."
            )
        if attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE:
            if snapshot.owner_attempt_id != attempt.attempt_id:
                raise ManualTransportRecoveryConflictError(
                    "Recovery Attempt is not the current material DROP ownership evidence."
                )
            return

        returns = list(self._session.scalars(
            select(ExecutionAttempt)
            .where(
                ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
            )
            .order_by(ExecutionAttempt.attempt_no.desc())
            .with_for_update()
        ))
        if not returns or returns[0].attempt_id != attempt.attempt_id:
            raise ManualTransportRecoveryConflictError(
                "Recovery Attempt is superseded by a newer empty-pallet return Attempt."
            )

    def _record_recovery_evidence(
        self, *,
        attempt: ExecutionAttempt,
        confirmed_location_code: str,
        operator_note: str | None,
    ) -> None:
        original: object | None = None
        if attempt.result_payload_json:
            try:
                original = json.loads(attempt.result_payload_json)
            except (TypeError, json.JSONDecodeError):
                original = {"unparseable_result_payload": attempt.result_payload_json}
        evidence: dict[str, object] = {
            "confirmed_location_code": confirmed_location_code,
            "recovered_at": self._utcnow().isoformat(),
        }
        if operator_note is not None:
            evidence["operator_note"] = operator_note
        payload: dict[str, object] = {OPERATOR_LOCATION_RECOVERY_KEY: evidence}
        if original is not None:
            payload["original_result"] = original
        attempt.result_payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _result(
        self, *,
        delivery: JobMaterialDelivery,
        attempt: ExecutionAttempt,
        location: str,
        recovery_applied: bool,
    ) -> ManualTransportRecoveryResult:
        snapshot: DropResourceSnapshot = self._drop_resource_service.get_drop_state()
        return ManualTransportRecoveryResult(
            delivery_id=delivery.job_delivery_id,
            attempt_id=attempt.attempt_id,
            command_type=attempt.command_type,
            confirmed_location_code=location,
            attempt_status=attempt.status,
            delivery_status=delivery.status,
            derived_drop_state=snapshot.state,
            recovery_applied=recovery_applied,
        )

    @staticmethod
    def _text(value: str, *, field_name: str) -> str:
        if not isinstance(value, str) or not (normalized := value.strip()):
            raise ManualTransportRecoveryError(f"{field_name} must be a non-empty string.")
        if len(normalized) > 100:
            raise ManualTransportRecoveryError(f"{field_name} must be 100 characters or fewer.")
        return normalized

    @staticmethod
    def _optional_text(value: str | None, *, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ManualTransportRecoveryError(f"{field_name} must be a string when supplied.")
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 1000:
            raise ManualTransportRecoveryError(f"{field_name} must be 1000 characters or fewer.")
        return normalized

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
