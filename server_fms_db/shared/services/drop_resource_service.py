"""Persistent ownership and transaction serialization for the shared DROP resource."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    SupplyMode,
)


DROP_CODE = "DROP"
# ASCII ``DROP`` in a documented, deterministic signed-bigint-safe namespace.
# Never use Python hash(), whose randomized value differs across processes.
DROP_RESOURCE_LOCK_KEY = 0x44524F50
MATERIAL_TRANSPORT_COMMAND_TYPE = "EXECUTE_TRANSPORT"
EMPTY_RETURN_COMMAND_TYPE = "EXECUTE_TRANSPORT_EMPTY_RETURN"
OPERATOR_LOCATION_RECOVERY_KEY = "operator_location_recovery"


class DropResourceState(StrEnum):
    FREE = "FREE"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    RETURNING = "RETURNING"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class DropResourceSnapshot:
    state: DropResourceState
    owner_delivery_id: int | None = None
    owner_attempt_id: int | None = None


class DropResourceUnavailableError(RuntimeError):
    def __init__(self, snapshot: DropResourceSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__(
            f"DROP is {snapshot.state.value}; owner_delivery_id={snapshot.owner_delivery_id}."
        )


class DropResourceOwnershipError(DropResourceUnavailableError):
    pass


class DropResourceService:
    """Derive DROP ownership from durable attempts; never retain process-local state.

    ``acquire_drop_transaction_guard`` is intentionally transaction-scoped.
    Callers hold it only while they re-check persisted state and create their
    own durable Attempt; adapter/network I/O happens after commit.
    """

    _ACTIVE_ATTEMPT_STATUSES = (
        ExecutionAttemptStatus.CREATED,
        ExecutionAttemptStatus.DISPATCHING,
        ExecutionAttemptStatus.ACCEPTED,
        ExecutionAttemptStatus.UNKNOWN,
    )
    _UNRESOLVED_TERMINAL_STATUSES = (
        ExecutionAttemptStatus.FAILED,
        ExecutionAttemptStatus.CANCELED,
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_drop_transaction_guard(self) -> None:
        """Serialize DROP claims across PostgreSQL processes for this transaction.

        SQLite has no advisory-lock primitive; local tests use the same durable
        resolver, while guarded PostgreSQL tests prove the production locking
        behavior.
        """
        if self._session.get_bind().dialect.name == "postgresql":
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
                {"key": DROP_RESOURCE_LOCK_KEY},
            )

    def get_drop_state(self) -> DropResourceSnapshot:
        """Rebuild the current state solely from committed/transactional Attempt evidence."""
        rows = self._session.execute(
            select(ExecutionAttempt, JobMaterialDelivery)
            .join(
                JobMaterialDelivery,
                ExecutionAttempt.job_delivery_id == JobMaterialDelivery.job_delivery_id,
            )
            .where(
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type.in_(
                    (MATERIAL_TRANSPORT_COMMAND_TYPE, EMPTY_RETURN_COMMAND_TYPE)
                ),
                JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
            )
        ).all()

        attempts_by_delivery: dict[int, list[ExecutionAttempt]] = {}
        for attempt, delivery in rows:
            if not self._attempt_uses_drop(attempt):
                continue
            attempts_by_delivery.setdefault(delivery.job_delivery_id, []).append(attempt)

        owners: list[DropResourceSnapshot] = []
        for delivery_id, attempts in attempts_by_delivery.items():
            state = self._state_for_delivery(delivery_id, attempts)
            if state is not None:
                owners.append(state)

        if not owners:
            return DropResourceSnapshot(state=DropResourceState.FREE)
        if len(owners) == 1:
            return owners[0]
        # Historical corruption/pre-Phase-C simultaneous attempts are never
        # silently assigned to one owner.  Keep the resource fail-closed.
        return DropResourceSnapshot(state=DropResourceState.AMBIGUOUS)

    def assert_drop_available_for_material(self) -> DropResourceSnapshot:
        snapshot = self.get_drop_state()
        if snapshot.state is not DropResourceState.FREE:
            raise DropResourceUnavailableError(snapshot)
        return snapshot

    def assert_delivery_owns_drop(self, *, job_delivery_id: int) -> DropResourceSnapshot:
        snapshot = self.get_drop_state()
        if snapshot.owner_delivery_id != job_delivery_id:
            raise DropResourceOwnershipError(snapshot)
        return snapshot

    def _state_for_delivery(
        self, delivery_id: int, attempts: list[ExecutionAttempt]
    ) -> DropResourceSnapshot | None:
        material_attempts = [
            attempt for attempt in attempts
            if attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE
        ]
        if not material_attempts:
            return None

        return_attempts = [
            attempt for attempt in attempts
            if attempt.command_type == EMPTY_RETURN_COMMAND_TYPE
        ]
        active_returns = [
            attempt for attempt in return_attempts
            if attempt.status in self._ACTIVE_ATTEMPT_STATUSES
        ]
        if active_returns:
            return DropResourceSnapshot(
                state=DropResourceState.RETURNING,
                owner_delivery_id=delivery_id,
                owner_attempt_id=max(active_returns, key=lambda attempt: attempt.attempt_no).attempt_id,
            )
        if any(attempt.status is ExecutionAttemptStatus.SUCCEEDED for attempt in return_attempts):
            return None

        material_active = [
            attempt for attempt in material_attempts
            if attempt.status in self._ACTIVE_ATTEMPT_STATUSES
        ]
        if material_active:
            return DropResourceSnapshot(
                state=DropResourceState.RESERVED,
                owner_delivery_id=delivery_id,
                owner_attempt_id=max(material_active, key=lambda attempt: attempt.attempt_no).attempt_id,
            )
        material_succeeded = [
            attempt for attempt in material_attempts
            if attempt.status is ExecutionAttemptStatus.SUCCEEDED
        ]
        if material_succeeded:
            return DropResourceSnapshot(
                state=DropResourceState.OCCUPIED,
                owner_delivery_id=delivery_id,
                owner_attempt_id=max(material_succeeded, key=lambda attempt: attempt.attempt_no).attempt_id,
            )

        # Current adapter terminal failures/cancellation do not durably prove
        # pallet location.  Until an explicit recovery policy exists they are
        # conservative reservations, not an implicit DROP release.
        unresolved = [
            attempt for attempt in material_attempts
            if attempt.status in self._UNRESOLVED_TERMINAL_STATUSES
            and not operator_confirmed_at_pickup(attempt)
        ]
        if unresolved:
            return DropResourceSnapshot(
                state=DropResourceState.RESERVED,
                owner_delivery_id=delivery_id,
                owner_attempt_id=max(unresolved, key=lambda attempt: attempt.attempt_no).attempt_id,
            )
        return None

    @staticmethod
    def _attempt_uses_drop(attempt: ExecutionAttempt) -> bool:
        try:
            payload = json.loads(attempt.request_payload_json)
        except (TypeError, json.JSONDecodeError):
            # A malformed legacy payload cannot establish that this particular
            # logical resource is free; excluding it avoids inventing route data.
            return False
        if not isinstance(payload, dict):
            return False
        if attempt.command_type == MATERIAL_TRANSPORT_COMMAND_TYPE:
            return payload.get("dropoff_code") == DROP_CODE
        return (
            payload.get("pickup_code") == DROP_CODE
            and isinstance(payload.get("dropoff_code"), str)
            and bool(payload["dropoff_code"].strip())
        )


def attempt_transport_route(attempt: ExecutionAttempt) -> tuple[str, str] | None:
    """Return persisted logical route only when it is complete and trustworthy."""
    try:
        payload = json.loads(attempt.request_payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pickup = payload.get("pickup_code")
    dropoff = payload.get("dropoff_code")
    if not isinstance(pickup, str) or not pickup.strip():
        return None
    if not isinstance(dropoff, str) or not dropoff.strip():
        return None
    return pickup.strip(), dropoff.strip()


def operator_location_recovery(attempt: ExecutionAttempt) -> dict[str, object] | None:
    """Read durable operator confirmation without treating arbitrary result JSON as recovery."""
    try:
        payload = json.loads(attempt.result_payload_json) if attempt.result_payload_json else None
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    recovery = payload.get(OPERATOR_LOCATION_RECOVERY_KEY)
    if not isinstance(recovery, dict):
        return None
    location = recovery.get("confirmed_location_code")
    if not isinstance(location, str) or not location.strip():
        return None
    return recovery


def operator_confirmed_at_pickup(attempt: ExecutionAttempt) -> bool:
    route = attempt_transport_route(attempt)
    evidence = operator_location_recovery(attempt)
    return bool(route and evidence and evidence.get("confirmed_location_code") == route[0])
