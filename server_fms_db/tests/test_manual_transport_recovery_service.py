from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.empty_pallet_return_service import EmptyPalletReturnService
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.manual_transport_recovery_service import (
    ManualTransportRecoveryConflictError,
    ManualTransportRecoveryService,
)
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery, JobMaterialDeliveryItem, MaterialInspection, MaterialInspectionStatus, MaterialInspectionResult, Part, PartCategory,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    OPERATOR_LOCATION_RECOVERY_KEY,
    DropResourceService,
    DropResourceState,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.transport_eligibility_service import TransportEligibilityService


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="RECOVERY_PRODUCT", product_name="Recovery product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def _delivery(session: Session, *, suffix: str, group: str = "OUTER_WALLS", status: MaterialDeliveryStatus = MaterialDeliveryStatus.IN_PROGRESS) -> JobMaterialDelivery:
    job = ProductionJob(job_code=f"RECOVERY_JOB_{suffix}", product_code="RECOVERY_PRODUCT", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"RECOVERY_DEL_{suffix}",
        display_name="Recovery delivery", status=status, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code=group, supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    part = Part(part_code=f"RECOVERY_PART_{suffix}", part_name="Recovery part", category=PartCategory.STRUCTURE, unit="EA", vision_class="wall_ext")
    session.add(part)
    session.flush()
    item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=None, part_code=part.part_code, quantity=1)
    session.add(item)
    session.flush()
    session.add(MaterialInspection(inspection_request_id=f"recovery-qa-{suffix}", delivery_item_id=item.delivery_item_id, inspection_cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, expected_part_code=part.part_code, expected_class_name=part.vision_class, expected_quantity=1, detected_quantity=1, production_valid=True))
    session.commit()
    return delivery


def _attempt(session: Session, delivery: JobMaterialDelivery, *, command_type: str = MATERIAL_TRANSPORT_COMMAND_TYPE, status: ExecutionAttemptStatus = ExecutionAttemptStatus.CREATED, attempt_no: int = 1) -> ExecutionAttempt:
    pickup, dropoff = ("DROP", "RACK1") if command_type == EMPTY_RETURN_COMMAND_TYPE else ("RACK1", "DROP")
    attempt = ExecutionAttempt(
        req_id=f"recovery-{delivery.job_delivery_id}-{command_type}-{attempt_no}",
        executor_type=ExecutorType.FORKLIFT, command_type=command_type,
        job_id=delivery.production_job_id, job_delivery_id=delivery.job_delivery_id,
        attempt_no=attempt_no, status=status,
        request_payload_json=json.dumps({"job_id": delivery.production_job_id, "delivery_id": delivery.job_delivery_id, "pickup_code": pickup, "dropoff_code": dropoff}),
    )
    session.add(attempt)
    session.commit()
    return attempt


def _recovery(session: Session, delivery: JobMaterialDelivery, attempt: ExecutionAttempt, location: str, note: str | None = None):
    return ManualTransportRecoveryService(session).confirm_location(
        production_job_id=delivery.production_job_id,
        job_delivery_id=delivery.job_delivery_id,
        attempt_id=attempt.attempt_id,
        confirmed_location_code=location,
        operator_note=note,
    )


def _empty_service(session: Session) -> EmptyPalletReturnService:
    coordinator = ForkliftExecutionCoordinator(
        session, adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    return EmptyPalletReturnService(session, forklift_execution_coordinator=coordinator)


def test_material_confirmed_at_pickup_releases_drop_and_allows_fresh_claim(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = _delivery(session, suffix="MAT-PICKUP")
    delivery.physical_ready_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    session.commit()
    attempt = _attempt(session, delivery)

    result = _recovery(session, delivery, attempt, "RACK1", "pallet visibly at source rack")
    assert result.attempt_status is ExecutionAttemptStatus.FAILED
    assert result.delivery_status is MaterialDeliveryStatus.PENDING
    assert result.derived_drop_state is DropResourceState.FREE
    stored = session.get(ExecutionAttempt, attempt.attempt_id)
    evidence = json.loads(stored.result_payload_json)[OPERATOR_LOCATION_RECOVERY_KEY]
    assert evidence["confirmed_location_code"] == "RACK1"
    assert evidence["operator_note"] == "pallet visibly at source rack"

    eligibility = TransportEligibilityService(session)
    monkeypatch.setattr(eligibility, "are_all_delivery_items_released", lambda _delivery: True)
    claim = eligibility.claim_transport(
        job_delivery_id=delivery.job_delivery_id,
        request_payload={"job_id": delivery.production_job_id, "delivery_id": delivery.job_delivery_id, "pickup_code": "RACK1", "dropoff_code": "DROP"},
    )
    assert claim.eligible is True
    assert claim.request_id != attempt.req_id
    assert claim.attempt_id != attempt.attempt_id


def test_material_confirmed_at_drop_completes_delivery_and_blocks_next_claim(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = _delivery(session, suffix="MAT-DROP")
    attempt = _attempt(session, delivery, status=ExecutionAttemptStatus.UNKNOWN)
    result = _recovery(session, delivery, attempt, "DROP")
    assert result.attempt_status is ExecutionAttemptStatus.SUCCEEDED
    assert result.delivery_status is MaterialDeliveryStatus.COMPLETED
    assert result.derived_drop_state is DropResourceState.OCCUPIED

    other = _delivery(session, suffix="OTHER", group="INNER_WALL", status=MaterialDeliveryStatus.PENDING)
    other.physical_ready_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    session.commit()
    eligibility = TransportEligibilityService(session)
    monkeypatch.setattr(eligibility, "are_all_delivery_items_released", lambda _delivery: True)
    claim = eligibility.claim_transport(
        job_delivery_id=other.job_delivery_id,
        request_payload={"job_id": other.production_job_id, "delivery_id": other.job_delivery_id, "pickup_code": "RACK2", "dropoff_code": "DROP"},
    )
    assert claim.eligible is False
    assert claim.reason.value == "DROP_RESOURCE_OCCUPIED"


def test_empty_return_confirmed_at_pickup_keeps_drop_occupied_and_can_be_retried(session: Session) -> None:
    delivery = _delivery(session, suffix="RETURN-PICKUP", status=MaterialDeliveryStatus.COMPLETED)
    _attempt(session, delivery, status=ExecutionAttemptStatus.SUCCEEDED)
    returning = _attempt(session, delivery, command_type=EMPTY_RETURN_COMMAND_TYPE, status=ExecutionAttemptStatus.UNKNOWN)

    result = _recovery(session, delivery, returning, "DROP")
    assert result.attempt_status is ExecutionAttemptStatus.FAILED
    assert result.delivery_status is MaterialDeliveryStatus.COMPLETED
    assert result.derived_drop_state is DropResourceState.OCCUPIED
    retry = _empty_service(session).claim_empty_pallet_return(job_delivery_id=delivery.job_delivery_id)
    assert retry.request_id != returning.req_id
    assert retry.attempt_id != returning.attempt_id


def test_empty_return_confirmed_at_rack_releases_drop_and_allows_next_material_claim(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = _delivery(session, suffix="RETURN-RACK", status=MaterialDeliveryStatus.COMPLETED)
    _attempt(session, delivery, status=ExecutionAttemptStatus.SUCCEEDED)
    returning = _attempt(session, delivery, command_type=EMPTY_RETURN_COMMAND_TYPE, status=ExecutionAttemptStatus.CREATED)

    result = _recovery(session, delivery, returning, "RACK1")
    assert result.attempt_status is ExecutionAttemptStatus.SUCCEEDED
    assert result.delivery_status is MaterialDeliveryStatus.COMPLETED
    assert result.derived_drop_state is DropResourceState.FREE

    other = _delivery(session, suffix="AFTER-RETURN", group="INNER_WALL", status=MaterialDeliveryStatus.PENDING)
    other.physical_ready_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    session.commit()
    eligibility = TransportEligibilityService(session)
    monkeypatch.setattr(eligibility, "are_all_delivery_items_released", lambda _delivery: True)
    assert eligibility.claim_transport(
        job_delivery_id=other.job_delivery_id,
        request_payload={"job_id": other.production_job_id, "delivery_id": other.job_delivery_id, "pickup_code": "RACK2", "dropoff_code": "DROP"},
    ).eligible is True


def test_ordinary_material_failure_remains_reserved_without_recovery_evidence(session: Session) -> None:
    material = _delivery(session, suffix="ORDINARY-MAT")
    _attempt(session, material, status=ExecutionAttemptStatus.FAILED)
    assert DropResourceService(session).get_drop_state().state is DropResourceState.RESERVED


def test_ordinary_empty_return_failure_remains_occupied_without_recovery_evidence(session: Session) -> None:
    returning_owner = _delivery(session, suffix="ORDINARY-RETURN", status=MaterialDeliveryStatus.COMPLETED)
    _attempt(session, returning_owner, status=ExecutionAttemptStatus.SUCCEEDED)
    _attempt(session, returning_owner, command_type=EMPTY_RETURN_COMMAND_TYPE, status=ExecutionAttemptStatus.FAILED)
    assert DropResourceService(session).get_drop_state().state is DropResourceState.OCCUPIED


def test_invalid_location_wrong_delivery_idempotency_and_conflicting_recovery_fail_closed(session: Session) -> None:
    delivery = _delivery(session, suffix="VALIDATION")
    attempt = _attempt(session, delivery)
    with pytest.raises(ManualTransportRecoveryConflictError):
        _recovery(session, delivery, attempt, "RACK2")
    assert session.get(ExecutionAttempt, attempt.attempt_id).status is ExecutionAttemptStatus.CREATED

    other = _delivery(session, suffix="OTHER")
    with pytest.raises(ManualTransportRecoveryConflictError):
        _recovery(session, other, attempt, "RACK1")

    first = _recovery(session, delivery, attempt, "RACK1")
    second = _recovery(session, delivery, attempt, "RACK1")
    assert first.recovery_applied is True
    assert second.recovery_applied is False
    with pytest.raises(ManualTransportRecoveryConflictError):
        _recovery(session, delivery, attempt, "DROP")
