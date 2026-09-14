from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    Product,
    RoofOptionCode,
)
from shared.services.pending_production_request_service import (
    ActivePendingProductionRequestExistsError,
    InvalidPendingProductionRequestInputError,
    InvalidPendingProductionRequestStateTransitionError,
    PendingProductionRequestService,
)
from shared.services.product_lookup_service import ProductNotFoundError


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    db.add_all(
        [
            Product(product_code="HOUSE_A", product_name="A형 초소형 주택"),
            Product(product_code="HOUSE_B", product_name="B형 초소형 주택"),
        ]
    )
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def service(session: Session, now: datetime, *, ttl_seconds: int = 300) -> PendingProductionRequestService:
    return PendingProductionRequestService(session, ttl_seconds=ttl_seconds, clock=lambda: now)


def test_create_without_roof_waits_for_roof_option(session: Session, now: datetime) -> None:
    pending = service(session, now).create_pending(
        session_id="session-1", product_code="HOUSE_A", quantity=1
    )

    assert pending.state is PendingProductionState.WAITING_ROOF_OPTION
    assert pending.roof_option_code is None
    assert pending.expires_at == now + timedelta(seconds=300)
    assert pending.created_at is not None


def test_create_with_roof_waits_for_confirmation(session: Session, now: datetime) -> None:
    pending = service(session, now).create_pending(
        session_id="session-2",
        product_code="HOUSE_B",
        quantity=2,
        roof_option_code=RoofOptionCode.ROOF_02,
    )

    assert pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert pending.roof_option_code is RoofOptionCode.ROOF_02
    assert pending.quantity == 2


@pytest.mark.parametrize("quantity", [0, -1, True])
def test_create_rejects_non_positive_quantity(session: Session, now: datetime, quantity: int) -> None:
    with pytest.raises(InvalidPendingProductionRequestInputError):
        service(session, now).create_pending(
            session_id="invalid-quantity", product_code="HOUSE_A", quantity=quantity
        )


def test_create_rejects_unknown_product(session: Session, now: datetime) -> None:
    with pytest.raises(ProductNotFoundError):
        service(session, now).create_pending(
            session_id="unknown-product", product_code="HOUSE_X", quantity=1
        )


def test_one_active_pending_per_session_and_other_sessions_are_allowed(
    session: Session, now: datetime
) -> None:
    svc = service(session, now)
    first = svc.create_pending(session_id="same-session", product_code="HOUSE_A", quantity=1)

    with pytest.raises(ActivePendingProductionRequestExistsError):
        svc.create_pending(session_id="same-session", product_code="HOUSE_B", quantity=1)

    assert session.scalar(select(PendingProductionRequest).where(PendingProductionRequest.request_id == first.request_id))
    assert session.scalar(select(PendingProductionRequest.request_id).where(PendingProductionRequest.session_id == "same-session")) == first.request_id
    second = svc.create_pending(session_id="other-session", product_code="HOUSE_B", quantity=1)
    assert second.request_id != first.request_id


def test_expired_request_is_not_active_and_history_allows_a_new_request(session: Session, now: datetime) -> None:
    current = {"value": now}
    svc = PendingProductionRequestService(
        session, ttl_seconds=1, clock=lambda: current["value"]
    )
    first = svc.create_pending(session_id="expiring-session", product_code="HOUSE_A", quantity=1)
    current["value"] = now + timedelta(seconds=2)

    assert svc.get_active_by_session("expiring-session") is None
    assert svc.get_by_id(first.request_id).state is PendingProductionState.EXPIRED
    second = svc.create_pending(session_id="expiring-session", product_code="HOUSE_B", quantity=1)
    assert second.request_id != first.request_id
    assert second.state is PendingProductionState.WAITING_ROOF_OPTION


def test_set_roof_option_transitions_waiting_request(session: Session, now: datetime) -> None:
    svc = service(session, now)
    pending = svc.create_pending(session_id="set-roof", product_code="HOUSE_A", quantity=1)

    changed = svc.set_roof_option(
        request_id=pending.request_id, roof_option_code=RoofOptionCode.ROOF_01
    )

    assert changed.state is PendingProductionState.AWAITING_CONFIRMATION
    assert changed.roof_option_code is RoofOptionCode.ROOF_01


def test_confirm_and_reject_pending_record_terminal_timestamps(session: Session, now: datetime) -> None:
    svc = service(session, now)
    confirmed = svc.create_pending(
        session_id="confirm", product_code="HOUSE_A", quantity=1, roof_option_code=RoofOptionCode.ROOF_01
    )
    confirmed = svc.confirm_pending(request_id=confirmed.request_id)
    assert confirmed.state is PendingProductionState.CONFIRMED
    assert confirmed.confirmed_at == now
    assert confirmed.rejected_at is None

    rejected = svc.create_pending(
        session_id="reject", product_code="HOUSE_B", quantity=1, roof_option_code=RoofOptionCode.ROOF_02
    )
    rejected = svc.reject_pending(request_id=rejected.request_id)
    assert rejected.state is PendingProductionState.REJECTED
    assert rejected.rejected_at == now
    assert rejected.confirmed_at is None


@pytest.mark.parametrize("method_name", ["confirm_pending", "reject_pending"])
def test_confirmation_transitions_reject_terminal_reentry(
    session: Session, now: datetime, method_name: str
) -> None:
    svc = service(session, now)
    pending = svc.create_pending(
        session_id=f"terminal-{method_name}", product_code="HOUSE_A", quantity=1, roof_option_code=RoofOptionCode.ROOF_01
    )
    getattr(svc, method_name)(request_id=pending.request_id)
    with pytest.raises(InvalidPendingProductionRequestStateTransitionError):
        getattr(svc, method_name)(request_id=pending.request_id)


def test_confirm_expired_request_persists_expiry_and_rejects_confirmation(session: Session, now: datetime) -> None:
    current = {"value": now}
    svc = PendingProductionRequestService(session, ttl_seconds=1, clock=lambda: current["value"])
    pending = svc.create_pending(
        session_id="expired-confirm", product_code="HOUSE_A", quantity=1, roof_option_code=RoofOptionCode.ROOF_01
    )
    current["value"] = now + timedelta(seconds=2)
    with pytest.raises(InvalidPendingProductionRequestStateTransitionError):
        svc.confirm_pending(request_id=pending.request_id)
    assert svc.get_by_id(pending.request_id).state is PendingProductionState.EXPIRED


def test_set_roof_option_persists_expiry_before_rejecting_transition(session: Session, now: datetime) -> None:
    current = {"value": now}
    svc = PendingProductionRequestService(session, ttl_seconds=1, clock=lambda: current["value"])
    pending = svc.create_pending(session_id="expired-set-roof", product_code="HOUSE_A", quantity=1)
    current["value"] = now + timedelta(seconds=2)

    with pytest.raises(InvalidPendingProductionRequestStateTransitionError):
        svc.set_roof_option(request_id=pending.request_id, roof_option_code=RoofOptionCode.ROOF_01)

    assert svc.get_by_id(pending.request_id).state is PendingProductionState.EXPIRED


@pytest.mark.parametrize(
    "state",
    [
        PendingProductionState.AWAITING_CONFIRMATION,
        PendingProductionState.CONFIRMED,
        PendingProductionState.REJECTED,
        PendingProductionState.EXPIRED,
    ],
)
def test_set_roof_option_rejects_invalid_state_transition(
    session: Session, now: datetime, state: PendingProductionState
) -> None:
    svc = service(session, now)
    pending = svc.create_pending(session_id=f"state-{state.value}", product_code="HOUSE_A", quantity=1)
    pending.state = state
    if state is PendingProductionState.AWAITING_CONFIRMATION:
        pending.roof_option_code = RoofOptionCode.ROOF_01
    session.commit()

    with pytest.raises(InvalidPendingProductionRequestStateTransitionError):
        svc.set_roof_option(request_id=pending.request_id, roof_option_code=RoofOptionCode.ROOF_02)


def test_create_rolls_back_when_flush_fails(session: Session, now: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(session, now)

    def fail_flush() -> None:
        raise RuntimeError("forced flush failure")

    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="forced flush failure"):
        svc.create_pending(session_id="rollback", product_code="HOUSE_A", quantity=1)

    assert session.scalar(select(PendingProductionRequest.request_id).where(PendingProductionRequest.session_id == "rollback")) is None
