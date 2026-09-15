"""Persistent, multi-turn production-request state without AI or job side effects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    RoofOptionCode,
)
from shared.services.product_lookup_service import ProductLookupService


class PendingProductionRequestError(RuntimeError):
    """Base error for pending production request domain operations."""


class InvalidPendingProductionRequestInputError(PendingProductionRequestError):
    """Raised when the caller supplies invalid pending request input."""


class PendingProductionRequestNotFoundError(PendingProductionRequestError):
    """Raised when a pending production request does not exist."""


class ActivePendingProductionRequestExistsError(PendingProductionRequestError):
    """Raised when a session already owns a non-expired active request."""


class InvalidPendingProductionRequestStateTransitionError(PendingProductionRequestError):
    """Raised when a pending request lifecycle transition is not allowed."""

    def __init__(self, *, current: PendingProductionState, target: PendingProductionState) -> None:
        super().__init__(f"Invalid pending production request state transition: {current.value} -> {target.value}.")


_Result = TypeVar("_Result")
_ACTIVE_STATES = frozenset(
    {PendingProductionState.COLLECTING_DETAILS, PendingProductionState.WAITING_ROOF_OPTION, PendingProductionState.AWAITING_CONFIRMATION}
)


class PendingProductionRequestService:
    """Persist and transition incomplete production requests one transaction at a time.

    This service deliberately does not parse natural language, emit response text, or
    create production jobs.  The caller supplies canonical values after those layers
    have finished their own work.
    """

    def __init__(
        self,
        session: Session,
        *,
        ttl_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        configured_ttl = (
            get_settings().pending_production_request_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        self._ttl_seconds = self._validate_ttl_seconds(configured_ttl)
        self._clock = clock or self._utcnow
        self._products = ProductLookupService(session)

    def create_pending(
        self,
        *,
        session_id: str,
        product_code: str | None,
        quantity: int,
        roof_option_code: RoofOptionCode | None = None,
    ) -> PendingProductionRequest:
        """Create one active request for a session, rejecting any unexpired predecessor."""

        normalized_session_id = self._normalize_session_id(session_id)
        normalized_quantity = self._validate_quantity(quantity)
        normalized_roof_option = self._normalize_roof_option_code(roof_option_code)

        def operation() -> PendingProductionRequest:
            product = self._products.get_by_code(product_code) if product_code is not None else None
            now = self._now()
            active = self._get_active_for_update(normalized_session_id)
            if active is not None:
                if self._expire_if_needed_locked(active, now=now):
                    self._session.flush()
                else:
                    raise ActivePendingProductionRequestExistsError(
                        f"An active pending production request already exists for session_id={normalized_session_id!r}."
                    )

            pending = PendingProductionRequest(
                session_id=normalized_session_id,
                state=(
                    PendingProductionState.COLLECTING_DETAILS if product is None
                    else PendingProductionState.WAITING_ROOF_OPTION
                    if normalized_roof_option is None
                    else PendingProductionState.AWAITING_CONFIRMATION
                ),
                product_code=product.product_code if product is not None else None,
                quantity=normalized_quantity,
                roof_option_code=normalized_roof_option,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            self._session.add(pending)
            self._session.flush()
            return pending

        try:
            return self._run_write_transaction(operation)
        except IntegrityError as error:
            # PostgreSQL's partial unique index is the final guard when two callers
            # race between their active-row checks.
            raise ActivePendingProductionRequestExistsError(
                f"An active pending production request already exists for session_id={normalized_session_id!r}."
            ) from error

    def get_by_id(self, request_id: int) -> PendingProductionRequest:
        """Return one request without changing its lifecycle state."""

        self._validate_request_id(request_id)
        pending = self._session.get(PendingProductionRequest, request_id)
        if pending is None:
            raise PendingProductionRequestNotFoundError(
                f"Pending production request not found: request_id={request_id}."
            )
        return pending

    def get_active_by_session(self, session_id: str) -> PendingProductionRequest | None:
        """Return the active request, expiring it atomically if its TTL has elapsed."""

        normalized_session_id = self._normalize_session_id(session_id)

        def operation() -> PendingProductionRequest | None:
            pending = self._get_active_for_update(normalized_session_id)
            if pending is None:
                return None
            if self._expire_if_needed_locked(pending, now=self._now()):
                self._session.flush()
                return None
            return pending

        return self._run_write_transaction(operation)

    def set_product(self, *, request_id: int, product_code: str) -> PendingProductionRequest:
        """Persist one canonical product into a collecting draft, idempotently."""
        self._validate_request_id(request_id)
        product = self._products.get_by_code(product_code)

        def operation() -> tuple[PendingProductionRequest, bool]:
            pending = self._get_by_id_for_update(request_id)
            if self._expire_if_needed_locked(pending, now=self._now()):
                self._session.flush()
                return pending, True
            if pending.state is PendingProductionState.COLLECTING_DETAILS:
                pending.product_code = product.product_code
                pending.state = (
                    PendingProductionState.WAITING_ROOF_OPTION
                    if pending.roof_option_code is None
                    else PendingProductionState.AWAITING_CONFIRMATION
                )
                self._session.flush()
                return pending, False
            if pending.product_code == product.product_code:
                return pending, False
            raise InvalidPendingProductionRequestStateTransitionError(
                current=pending.state, target=PendingProductionState.WAITING_ROOF_OPTION
            )

        pending, expired = self._run_write_transaction(operation)
        if expired:
            raise InvalidPendingProductionRequestStateTransitionError(
                current=PendingProductionState.EXPIRED, target=PendingProductionState.WAITING_ROOF_OPTION
            )
        return pending

    def set_roof_option(
        self, *, request_id: int, roof_option_code: RoofOptionCode
    ) -> PendingProductionRequest:
        """Move a waiting request to confirmation-ready using a canonical roof value."""

        self._validate_request_id(request_id)
        normalized_roof_option = self._normalize_roof_option_code(roof_option_code)
        if normalized_roof_option is None:
            raise InvalidPendingProductionRequestInputError("roof_option_code must not be None.")

        def operation() -> tuple[PendingProductionRequest, bool]:
            pending = self._get_by_id_for_update(request_id)
            if self._expire_if_needed_locked(pending, now=self._now()):
                # Return after flush so the enclosing transaction commits EXPIRED;
                # raising here would roll the expiry update back.
                self._session.flush()
                return pending, True
            if pending.state is not PendingProductionState.WAITING_ROOF_OPTION:
                raise InvalidPendingProductionRequestStateTransitionError(
                    current=pending.state,
                    target=PendingProductionState.AWAITING_CONFIRMATION,
                )
            pending.roof_option_code = normalized_roof_option
            pending.state = PendingProductionState.AWAITING_CONFIRMATION
            self._session.flush()
            return pending, False

        pending, expired = self._run_write_transaction(operation)
        if expired:
            raise InvalidPendingProductionRequestStateTransitionError(
                current=PendingProductionState.EXPIRED,
                target=PendingProductionState.AWAITING_CONFIRMATION,
            )
        return pending

    def confirm_pending(self, *, request_id: int) -> PendingProductionRequest:
        """Persist confirmation for a non-expired confirmation-ready request only."""

        return self._transition_confirmation(request_id=request_id, target=PendingProductionState.CONFIRMED)

    def reject_pending(self, *, request_id: int) -> PendingProductionRequest:
        """Persist rejection for a non-expired confirmation-ready request only."""

        return self._transition_confirmation(request_id=request_id, target=PendingProductionState.REJECTED)

    def _transition_confirmation(
        self, *, request_id: int, target: PendingProductionState
    ) -> PendingProductionRequest:
        self._validate_request_id(request_id)

        def operation() -> tuple[PendingProductionRequest, bool]:
            pending = self._get_by_id_for_update(request_id)
            now = self._now()
            if self._expire_if_needed_locked(pending, now=now):
                self._session.flush()
                return pending, True
            if pending.state is not PendingProductionState.AWAITING_CONFIRMATION:
                raise InvalidPendingProductionRequestStateTransitionError(
                    current=pending.state,
                    target=target,
                )
            pending.state = target
            pending.updated_at = now
            if target is PendingProductionState.CONFIRMED:
                pending.confirmed_at = now
                pending.rejected_at = None
            else:
                pending.rejected_at = now
                pending.confirmed_at = None
            self._session.flush()
            return pending, False

        pending, expired = self._run_write_transaction(operation)
        if expired:
            raise InvalidPendingProductionRequestStateTransitionError(
                current=PendingProductionState.EXPIRED,
                target=target,
            )
        return pending

    def expire_if_needed(self, request_id: int) -> PendingProductionRequest:
        """Persist EXPIRED for an elapsed active request and return its current state."""

        self._validate_request_id(request_id)

        def operation() -> PendingProductionRequest:
            pending = self._get_by_id_for_update(request_id)
            if self._expire_if_needed_locked(pending, now=self._now()):
                self._session.flush()
            return pending

        return self._run_write_transaction(operation)

    def _get_active_for_update(self, session_id: str) -> PendingProductionRequest | None:
        return self._session.scalar(
            select(PendingProductionRequest)
            .where(
                PendingProductionRequest.session_id == session_id,
                PendingProductionRequest.state.in_(_ACTIVE_STATES),
            )
            .order_by(PendingProductionRequest.request_id.desc())
            .with_for_update()
        )

    def _get_by_id_for_update(self, request_id: int) -> PendingProductionRequest:
        pending = self._session.scalar(
            select(PendingProductionRequest)
            .where(PendingProductionRequest.request_id == request_id)
            .with_for_update()
        )
        if pending is None:
            raise PendingProductionRequestNotFoundError(
                f"Pending production request not found: request_id={request_id}."
            )
        return pending

    @staticmethod
    def _expire_if_needed_locked(pending: PendingProductionRequest, *, now: datetime) -> bool:
        expires_at = pending.expires_at
        # SQLite does not round-trip timezone information for DateTime(timezone=True),
        # while PostgreSQL does. Treat its stored UTC value consistently in unit tests.
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if pending.state in _ACTIVE_STATES and expires_at <= now:
            pending.state = PendingProductionState.EXPIRED
            return True
        return False

    def _run_write_transaction(self, operation: Callable[[], _Result]) -> _Result:
        try:
            result = operation()
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidPendingProductionRequestInputError("clock must return a timezone-aware datetime.")
        return value

    @staticmethod
    def _validate_ttl_seconds(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidPendingProductionRequestInputError(
                "pending production request TTL must be a positive integer."
            )
        return value

    @staticmethod
    def _validate_request_id(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidPendingProductionRequestInputError("request_id must be a positive integer.")

    @staticmethod
    def _validate_quantity(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidPendingProductionRequestInputError("quantity must be a positive integer.")
        return value

    @staticmethod
    def _normalize_session_id(value: str) -> str:
        if not isinstance(value, str) or not (normalized := value.strip()):
            raise InvalidPendingProductionRequestInputError("session_id must be a non-empty string.")
        if len(normalized) > 100:
            raise InvalidPendingProductionRequestInputError("session_id must be 100 characters or fewer.")
        return normalized

    @staticmethod
    def _normalize_roof_option_code(
        value: RoofOptionCode | None,
    ) -> RoofOptionCode | None:
        if value is None:
            return None
        if not isinstance(value, RoofOptionCode):
            raise InvalidPendingProductionRequestInputError(
                "roof_option_code must be a RoofOptionCode or None."
            )
        return value

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
