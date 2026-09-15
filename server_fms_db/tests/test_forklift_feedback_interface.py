from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionFeedback,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.models import Base
from shared.models.factory import Product
from shared.services.execution_attempt_service import ExecutionAttemptService


def _execute(adapter: ForkliftActionAdapter, callback=None) -> ForkliftExecutionResult:
    return adapter.dispatch_execute_transport(
        req_id="TEST-FEEDBACK-EXECUTE",
        job_id=42,
        delivery_id=7,
        pickup_code="RACK1",
        dropoff_code="DROP",
        feedback_callback=callback,
    )


def test_fake_transport_without_feedback_preserves_result_and_emits_nothing() -> None:
    seen: list[ForkliftExecutionFeedback] = []
    result = _execute(ForkliftActionAdapter(FakeForkliftActionTransport()), seen.append)

    assert result.status == ForkliftActionStatus.SUCCEEDED
    assert seen == []


def test_fake_execute_transport_delivers_configured_feedback_in_order() -> None:
    expected = (
        ForkliftExecutionFeedback("MOVING_TO_PICKUP", 0.1, "heading to pallet"),
        ForkliftExecutionFeedback("MOVING_TO_DROPOFF", 0.6, "heading to line"),
    )
    seen: list[ForkliftExecutionFeedback] = []

    _execute(
        ForkliftActionAdapter(FakeForkliftActionTransport(execute_transport_feedback=expected)),
        seen.append,
    )

    assert seen == list(expected)
    assert [item.phase for item in seen] == ["MOVING_TO_PICKUP", "MOVING_TO_DROPOFF"]
    assert [item.progress for item in seen] == [0.1, 0.6]
    assert [item.detail for item in seen] == ["heading to pallet", "heading to line"]


@pytest.mark.parametrize("status", [ForkliftActionStatus.FAILED, ForkliftActionStatus.CANCELED])
def test_fake_execute_transport_preserves_terminal_result(status: ForkliftActionStatus) -> None:
    configured = ForkliftExecutionResult(status, "E-TEST", "configured result")
    result = _execute(
        ForkliftActionAdapter(FakeForkliftActionTransport(execute_transport_result=configured))
    )

    assert result == configured


def test_return_home_supports_feedback_and_preserves_result() -> None:
    expected = ForkliftExecutionFeedback("RETURNING_HOME", 0.5, "returning")
    configured = ForkliftExecutionResult(ForkliftActionStatus.CANCELED, "", "canceled")
    transport = FakeForkliftActionTransport(
        return_home_feedback=(expected,),
        return_home_result=configured,
    )
    seen: list[ForkliftExecutionFeedback] = []

    result = ForkliftActionAdapter(transport).dispatch_return_home("TEST-FEEDBACK-HOME", seen.append)

    assert seen == [expected]
    assert result == configured
    assert transport.return_home_requests == [{"req_id": "TEST-FEEDBACK-HOME"}]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_A", product_name="A"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_coordinator_forwards_raw_feedback_only_when_sink_is_configured(session: Session) -> None:
    expected = ForkliftExecutionFeedback("LIFTING_UP", 0.3, "lifting")
    transport = FakeForkliftActionTransport(execute_transport_feedback=(expected,))
    seen: list[ForkliftExecutionFeedback] = []
    coordinator = ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(transport),
        execution_attempt_service=ExecutionAttemptService(session),
        feedback_sink=seen.append,
    )

    coordinator.execute_transport(
        job_id=1,
        delivery_id=2,
        pickup_code="RACK1",
        dropoff_code="DROP",
        req_id="TEST-COORDINATOR-FEEDBACK",
    )

    assert seen == [expected]
