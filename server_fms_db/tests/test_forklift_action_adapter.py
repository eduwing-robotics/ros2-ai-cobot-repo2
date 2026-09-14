import pytest

from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftAdapterError,
)

def test_execute_transport_accepts_integer_ids() -> None:
    transport = FakeForkliftActionTransport()
    adapter = ForkliftActionAdapter(transport)

    result = adapter.dispatch_execute_transport(
        req_id="REQ-123",
        job_id=42,
        delivery_id=7,
        pickup_code="RACK1",
        dropoff_code="DROP",
    )

    assert result.status == ForkliftActionStatus.SUCCEEDED
    assert len(transport.execute_transport_requests) == 1

    req = transport.execute_transport_requests[0]
    assert req["req_id"] == "REQ-123"
    assert req["job_id"] == 42
    assert req["delivery_id"] == 7
    assert req == {"req_id": "REQ-123", "job_id": 42, "delivery_id": 7, "pickup_code": "RACK1", "dropoff_code": "DROP"}

def test_execute_transport_rejects_string_ids() -> None:
    transport = FakeForkliftActionTransport()
    adapter = ForkliftActionAdapter(transport)

    with pytest.raises(ForkliftAdapterError, match="job_id must be int"):
        adapter.dispatch_execute_transport(
            req_id="REQ-123",
            job_id="JOB-42",  # type: ignore
            delivery_id=7,
            pickup_code="RACK1",
            dropoff_code="DROP",
        )

    with pytest.raises(ForkliftAdapterError, match="delivery_id must be int"):
        adapter.dispatch_execute_transport(
            req_id="REQ-123",
            job_id=42,
            delivery_id="DEL-07",  # type: ignore
            pickup_code="RACK1",
            dropoff_code="DROP",
        )

def test_return_home() -> None:
    transport = FakeForkliftActionTransport()
    adapter = ForkliftActionAdapter(transport)

    result = adapter.dispatch_return_home(req_id="REQ-HOME-1")
    assert result.status == ForkliftActionStatus.SUCCEEDED

    assert len(transport.return_home_requests) == 1
    assert transport.return_home_requests[0]["req_id"] == "REQ-HOME-1"
