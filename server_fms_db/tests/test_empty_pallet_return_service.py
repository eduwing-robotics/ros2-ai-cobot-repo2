import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.automatic_empty_pallet_return_service import AutomaticEmptyPalletReturnService
from fms_server.empty_pallet_return_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    EmptyPalletReturnDuplicateError,
    EmptyPalletReturnNotEligibleError,
    EmptyPalletReturnService,
)
from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
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
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.realtime.production_events import set_production_change_callback


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="TEST_PRODUCT", product_name="Test Product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _delivery(
    session: Session,
    *,
    group: str = "OUTER_WALLS",
    status: MaterialDeliveryStatus = MaterialDeliveryStatus.COMPLETED,
    supply_mode: SupplyMode = SupplyMode.TRANSPORTED,
) -> JobMaterialDelivery:
    job = ProductionJob(product_code="TEST_PRODUCT", job_code=f"JOB-{group}-{supply_mode.value}", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        status=status,
        batch_order=1,
        delivery_code=f"DELIVERY-{group}",
        display_name="Test delivery",
        supply_mode=supply_mode,
        supply_group_code=group,
        supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    _material_attempt(session, delivery)
    session.commit()
    return delivery


def _material_attempt(session: Session, delivery: JobMaterialDelivery) -> ExecutionAttempt:
    attempt = ExecutionAttempt(
        req_id=f"material-{delivery.job_delivery_id}",
        executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT",
        job_id=delivery.production_job_id,
        job_delivery_id=delivery.job_delivery_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({"pickup_code": "RACK", "dropoff_code": "DROP"}),
    )
    session.add(attempt)
    session.flush()
    return attempt


def _service(
    session: Session,
    *,
    result: ForkliftExecutionResult | None = None,
) -> tuple[EmptyPalletReturnService, FakeForkliftActionTransport]:
    transport = FakeForkliftActionTransport(execute_transport_result=result)
    coordinator = ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(transport),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    return (
        EmptyPalletReturnService(session, forklift_execution_coordinator=coordinator),
        transport,
    )


def _return_attempts(session: Session, delivery_id: int) -> list[ExecutionAttempt]:
    return list(session.scalars(
        select(ExecutionAttempt)
        .where(
            ExecutionAttempt.job_delivery_id == delivery_id,
            ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
        )
        .order_by(ExecutionAttempt.attempt_no)
    ))


def test_outer_walls_empty_return_is_separate_logical_attempt_and_keeps_delivery_completed(session: Session) -> None:
    delivery = _delivery(session, group="OUTER_WALLS")
    service, transport = _service(session)

    claim = service.claim_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)
    assert claim.locations.pickup_code == "DROP"
    assert claim.locations.dropoff_code == "RACK1"
    assert claim.request_id != f"material-{delivery.job_delivery_id}"
    assert session.get(JobMaterialDelivery, delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED
    pending = _return_attempts(session, delivery.job_delivery_id)
    assert len(pending) == 1
    assert pending[0].status is ExecutionAttemptStatus.CREATED
    assert pending[0].command_type == EMPTY_RETURN_COMMAND_TYPE

    result = service._forklift_execution_coordinator.execute_transport(
        job_id=claim.job_id, delivery_id=claim.delivery_id,
        pickup_code=claim.locations.pickup_code, dropoff_code=claim.locations.dropoff_code,
        req_id=claim.request_id, attempt_preclaimed=True, command_type=EMPTY_RETURN_COMMAND_TYPE,
    )
    session.commit()

    assert result.status is ForkliftActionStatus.SUCCEEDED
    assert transport.execute_transport_requests == [{
        "req_id": claim.request_id, "job_id": claim.job_id, "delivery_id": claim.delivery_id,
        "pickup_code": "DROP", "dropoff_code": "RACK1",
    }]
    assert set(transport.execute_transport_requests[0]) == {"req_id", "job_id", "delivery_id", "pickup_code", "dropoff_code"}
    attempt = _return_attempts(session, delivery.job_delivery_id)[0]
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert json.loads(attempt.request_payload_json) == {
        "req_id": claim.request_id, "job_id": delivery.production_job_id,
        "delivery_id": delivery.job_delivery_id, "pickup_code": "DROP", "dropoff_code": "RACK1",
    }
    assert session.get(JobMaterialDelivery, delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED


def test_inner_wall_empty_return_dispatches_drop_to_rack_b(session: Session) -> None:
    delivery = _delivery(session, group="INNER_WALL")
    service, transport = _service(session)

    result = service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert result.status is ForkliftActionStatus.SUCCEEDED
    assert transport.execute_transport_requests[0]["pickup_code"] == "DROP"
    assert transport.execute_transport_requests[0]["dropoff_code"] == "RACK2"
    assert _return_attempts(session, delivery.job_delivery_id)[0].status is ExecutionAttemptStatus.SUCCEEDED
    assert session.get(JobMaterialDelivery, delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED


def test_successful_empty_return_notifies_production_after_commit_for_each_pallet_group(session: Session) -> None:
    observed: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: observed.append((job_id, reason)))
    try:
        for group in ("OUTER_WALLS", "INNER_WALL"):
            delivery = _delivery(session, group=group)
            service, _transport = _service(session)
            result = service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)
            assert result.status is ForkliftActionStatus.SUCCEEDED
            assert observed.pop(0) == (delivery.production_job_id, "empty_pallet_return_completed")
            assert _return_attempts(session, delivery.job_delivery_id)[0].status is ExecutionAttemptStatus.SUCCEEDED
        assert observed == []
    finally:
        set_production_change_callback(None)


def test_failed_empty_return_does_not_notify_production(session: Session) -> None:
    observed: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: observed.append((job_id, reason)))
    try:
        delivery = _delivery(session)
        service, _transport = _service(
            session,
            result=ForkliftExecutionResult(
                status=ForkliftActionStatus.FAILED, error_code="FAKE_FAILURE", detail="fake return failure"
            ),
        )
        result = service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)
        assert result.status is ForkliftActionStatus.FAILED
        assert observed == []
        assert _return_attempts(session, delivery.job_delivery_id)[0].status is ExecutionAttemptStatus.FAILED
    finally:
        set_production_change_callback(None)


def test_active_empty_return_duplicate_is_rejected_without_second_attempt_or_dispatch(session: Session) -> None:
    delivery = _delivery(session)
    service, transport = _service(session)
    service.claim_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    with pytest.raises(EmptyPalletReturnDuplicateError, match="active empty-pallet return"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert len(_return_attempts(session, delivery.job_delivery_id)) == 1
    assert transport.execute_transport_requests == []
    assert session.get(JobMaterialDelivery, delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED


def test_succeeded_empty_return_is_not_restarted(session: Session) -> None:
    delivery = _delivery(session)
    service, transport = _service(session)
    first = service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)
    assert first.status is ForkliftActionStatus.SUCCEEDED

    with pytest.raises(EmptyPalletReturnDuplicateError, match="already succeeded"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert len(_return_attempts(session, delivery.job_delivery_id)) == 1
    assert len(transport.execute_transport_requests) == 1


@pytest.mark.parametrize("status", [
    MaterialDeliveryStatus.PENDING,
    MaterialDeliveryStatus.IN_PROGRESS,
    MaterialDeliveryStatus.FAILED,
])
def test_empty_return_requires_completed_material_delivery(session: Session, status: MaterialDeliveryStatus) -> None:
    delivery = _delivery(session, status=status)
    service, transport = _service(session)

    with pytest.raises(EmptyPalletReturnNotEligibleError, match="COMPLETED"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert _return_attempts(session, delivery.job_delivery_id) == []
    assert transport.execute_transport_requests == []


def test_manual_delivery_is_never_dispatched_for_empty_return(session: Session) -> None:
    delivery = _delivery(session, supply_mode=SupplyMode.MANUAL)
    service, transport = _service(session)

    with pytest.raises(EmptyPalletReturnNotEligibleError, match="TRANSPORTED"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert _return_attempts(session, delivery.job_delivery_id) == []
    assert transport.execute_transport_requests == []


def test_terminal_job_rejects_empty_return_without_dispatch(session: Session) -> None:
    delivery = _delivery(session)
    session.get(ProductionJob, delivery.production_job_id).status = JobStatus.CANCELED
    session.commit()
    service, transport = _service(session)

    with pytest.raises(EmptyPalletReturnNotEligibleError, match="terminal production job"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert _return_attempts(session, delivery.job_delivery_id) == []
    assert transport.execute_transport_requests == []

def test_unknown_supply_group_fails_closed_without_dispatch(session: Session) -> None:
    delivery = _delivery(session, group="UNKNOWN_GROUP")
    service, transport = _service(session)

    with pytest.raises(EmptyPalletReturnNotEligibleError, match="Unsupported supply_group_code"):
        service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert _return_attempts(session, delivery.job_delivery_id) == []
    assert transport.execute_transport_requests == []


def test_failed_empty_return_marks_only_its_attempt_failed_and_keeps_delivery_completed(session: Session) -> None:
    delivery = _delivery(session)
    service, transport = _service(
        session,
        result=ForkliftExecutionResult(
            status=ForkliftActionStatus.FAILED, error_code="FAKE_FAILURE", detail="fake return failure",
        ),
    )

    result = service.execute_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)

    assert result.status is ForkliftActionStatus.FAILED
    assert len(transport.execute_transport_requests) == 1
    attempt = _return_attempts(session, delivery.job_delivery_id)[0]
    assert attempt.status is ExecutionAttemptStatus.FAILED
    assert attempt.error_code == "FAKE_FAILURE"
    assert session.get(JobMaterialDelivery, delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED


def test_automatic_candidate_query_excludes_completed_delivery_on_terminal_parent(session: Session) -> None:
    terminal = _delivery(session, group="OUTER_WALLS")
    active = _delivery(session, group="INNER_WALL")
    session.get(ProductionJob, terminal.production_job_id).status = JobStatus.CANCELED
    session.commit()

    candidate_ids = list(session.scalars(
        AutomaticEmptyPalletReturnService._candidate_delivery_ids_query()
    ))

    assert candidate_ids == [active.job_delivery_id]
    assert terminal.job_delivery_id not in candidate_ids
