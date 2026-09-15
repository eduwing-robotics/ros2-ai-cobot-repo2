from __future__ import annotations

import json
from collections.abc import Generator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    SupplyMode,
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add(Product(product_code="RECOVERY_API_PRODUCT", product_name="Recovery API product"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield db_session
    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _context(session: Session):
    job = ProductionJob(job_code="RECOVERY_API_JOB", product_code="RECOVERY_API_PRODUCT", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="RECOVERY_API_DEL", display_name="Recovery API delivery",
        status=MaterialDeliveryStatus.IN_PROGRESS, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    attempt = ExecutionAttempt(
        req_id="recovery-api-attempt", executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT", job_id=job.job_id, job_delivery_id=delivery.job_delivery_id,
        attempt_no=1, status=ExecutionAttemptStatus.CREATED,
        request_payload_json=json.dumps({"job_id": job.job_id, "delivery_id": delivery.job_delivery_id, "pickup_code": "RACK1", "dropoff_code": "DROP"}),
    )
    session.add(attempt)
    session.commit()
    return job, delivery, attempt


def _url(job, delivery) -> str:
    return f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}/transport-recovery"


def test_transport_recovery_api_accepts_only_physical_location_and_does_not_dispatch(client: TestClient, db_session: Session) -> None:
    job, delivery, attempt = _context(db_session)
    response = client.post(_url(job, delivery), json={
        "attempt_id": attempt.attempt_id,
        "confirmed_location_code": "RACK1",
        "operator_note": "visually verified",
    })
    assert response.status_code == 200
    assert response.json() == {
        "job_id": job.job_id, "delivery_id": delivery.job_delivery_id, "attempt_id": attempt.attempt_id,
        "command_type": "EXECUTE_TRANSPORT", "confirmed_location_code": "RACK1",
        "attempt_status": "FAILED", "delivery_status": "PENDING", "derived_drop_state": "FREE",
        "recovery_applied": True,
    }
    repeat = client.post(_url(job, delivery), json={"attempt_id": attempt.attempt_id, "confirmed_location_code": "RACK1"})
    assert repeat.status_code == 200 and repeat.json()["recovery_applied"] is False
    assert client.post(_url(job, delivery), json={
        "attempt_id": attempt.attempt_id, "confirmed_location_code": "DROP"
    }).status_code == 409



def test_transport_recovery_api_closes_durable_success_delivery_crash_window(
    client: TestClient, db_session: Session
) -> None:
    job, delivery, attempt = _context(db_session)
    # Model the only supported crash window: the independently committed
    # terminal Attempt is visible, but Delivery completion was never committed
    # before the process died.
    attempt.status = ExecutionAttemptStatus.SUCCEEDED
    delivery.status = MaterialDeliveryStatus.IN_PROGRESS
    db_session.commit()

    response = client.post(_url(job, delivery), json={
        "attempt_id": attempt.attempt_id,
        "confirmed_location_code": "DROP",
        "operator_note": "pallet visually confirmed at DROP after restart",
    })

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job.job_id,
        "delivery_id": delivery.job_delivery_id,
        "attempt_id": attempt.attempt_id,
        "command_type": "EXECUTE_TRANSPORT",
        "confirmed_location_code": "DROP",
        "attempt_status": "SUCCEEDED",
        "delivery_status": "COMPLETED",
        "derived_drop_state": "OCCUPIED",
        "recovery_applied": True,
    }
    db_session.refresh(delivery)
    db_session.refresh(attempt)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert "operator_location_recovery" in attempt.result_payload_json

def test_transport_recovery_api_rejects_status_override_wrong_attempt_and_invalid_location(client: TestClient, db_session: Session) -> None:
    job, delivery, attempt = _context(db_session)
    assert client.post(_url(job, delivery), json={
        "attempt_id": attempt.attempt_id, "confirmed_location_code": "RACK1", "attempt_status": "SUCCEEDED"
    }).status_code == 422
    assert client.post(_url(job, delivery), json={
        "attempt_id": attempt.attempt_id, "confirmed_location_code": "RACK2"
    }).status_code == 409
    assert client.post(_url(job, delivery), json={
        "attempt_id": 999999, "confirmed_location_code": "RACK1"
    }).status_code == 404
