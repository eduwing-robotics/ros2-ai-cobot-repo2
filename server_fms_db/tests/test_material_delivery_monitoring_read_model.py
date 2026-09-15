from __future__ import annotations

from collections.abc import Generator
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
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
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionEvent,
    ProductionJob,
    StepStatus,
    SupplyMode,
)


@pytest.fixture(name="db_session")
def db_session_fixture() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="client")
def client_fixture(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _delivery(
    session: Session,
    *,
    suffix: str,
    item_count: int = 1,
    mode: SupplyMode | None = SupplyMode.TRANSPORTED,
    group: str | None = "QA_MONITOR_GROUP",
    status: MaterialDeliveryStatus = MaterialDeliveryStatus.PENDING,
    job: ProductionJob | None = None,
) -> tuple[ProductionJob, JobMaterialDelivery, list[JobMaterialDeliveryItem]]:
    if job is None:
        product = Product(product_code=f"QA_MONITOR_PRODUCT_{suffix}", product_name="QA monitoring")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=f"QA_MONITOR_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=session.scalar(select(func.count()).select_from(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id)) + 1,
        delivery_code=f"QA_MONITOR_DEL_{suffix}",
        display_name=f"QA monitoring {suffix}",
        status=status,
        supply_mode=mode,
        supply_group_code=group,
    )
    session.add(delivery)
    session.flush()
    items: list[JobMaterialDeliveryItem] = []
    for index in range(item_count):
        part = Part(
            part_code=f"QA_MONITOR_PART_{suffix}_{index}",
            part_name="QA monitoring part",
            category=PartCategory.STRUCTURE,
            unit="EA",
            vision_class=f"qa_monitor_class_{suffix}_{index}",
        )
        session.add(part)
        session.flush()
        step = JobStep(
            job_id=job.job_id,
            step_order=session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id == job.job_id)) + 1,
            operation_code="INSTALL_TEST",
            display_name="QA monitoring step",
            part_code=part.part_code,
            quantity=1,
            vision_class=part.vision_class,
            supply_mode=mode,
            supply_group_code=group,
            status=StepStatus.PENDING,
        )
        session.add(step)
        session.flush()
        item = JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id,
            job_step_id=step.job_step_id,
            part_code=part.part_code,
            quantity=1,
        )
        session.add(item)
        items.append(item)
    session.commit()
    return job, delivery, items


def _inspection(
    session: Session,
    item: JobMaterialDeliveryItem,
    *,
    cycle: int,
    status: MaterialInspectionStatus,
    result: MaterialInspectionResult | None = None,
    production_valid: bool | None = None,
    failure_reason: str | None = None,
) -> MaterialInspection:
    step = session.get(JobStep, item.job_step_id)
    assert step is not None
    row = MaterialInspection(
        inspection_request_id=f"qa-monitor-{item.delivery_item_id}-{cycle}",
        delivery_item_id=item.delivery_item_id,
        inspection_cycle=cycle,
        status=status,
        result=result,
        expected_part_code=item.part_code,
        expected_class_name=step.vision_class or "missing",
        expected_quantity=item.quantity,
        production_valid=production_valid,
        failure_reason=failure_reason,
        completed_at=datetime.now(timezone.utc) if status in {MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR} else None,
    )
    session.add(row)
    session.commit()
    return row


def _body(client: TestClient, job: ProductionJob) -> list[dict[str, object]]:
    response = client.get(f"/production/jobs/{job.job_id}/material-deliveries")
    assert response.status_code == 200
    return response.json()


def test_not_requested_monitoring_is_zero_write_and_exposes_transport_policy(
    client: TestClient, db_session: Session,
) -> None:
    job, delivery, items = _delivery(db_session, suffix="NONE")
    before = (
        db_session.scalar(select(func.count()).select_from(MaterialInspection)),
        db_session.scalar(select(func.count()).select_from(ExecutionAttempt)),
        db_session.scalar(select(func.count()).select_from(ProductionEvent)),
    )

    body = _body(client, job)

    after = (
        db_session.scalar(select(func.count()).select_from(MaterialInspection)),
        db_session.scalar(select(func.count()).select_from(ExecutionAttempt)),
        db_session.scalar(select(func.count()).select_from(ProductionEvent)),
    )
    entry = body[0]
    item = entry["items"][0]
    assert before == after == (0, 0, 0)
    assert entry["job_delivery_id"] == delivery.job_delivery_id
    assert entry["status"] == "PENDING"
    assert entry["physical_ready"] is False
    assert entry["qa_applicable"] is True
    assert entry["qa_total_items"] == 1
    assert entry["qa_released_items"] == 0
    assert entry["qa_all_released"] is False
    assert item["delivery_item_id"] == items[0].delivery_item_id
    assert item["vision_class"] == "qa_monitor_class_NONE_0"
    assert item["expected_quantity"] == 1
    assert item["qa_state"] == "NOT_REQUESTED"
    assert item["qa_released"] is False
    assert item["latest_inspection"] is None


def _transport_attempt(
    session: Session,
    *,
    delivery: JobMaterialDelivery,
    command_type: str = "EXECUTE_TRANSPORT",
    status: ExecutionAttemptStatus = ExecutionAttemptStatus.SUCCEEDED,
) -> ExecutionAttempt:
    payload = (
        {"pickup_code": "DROP", "dropoff_code": "RACK1"}
        if command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN"
        else {"pickup_code": "RACK1", "dropoff_code": "DROP"}
    )
    attempt = ExecutionAttempt(
        req_id=f"monitor-{command_type}-{delivery.job_delivery_id}-{status.value}",
        executor_type=ExecutorType.FORKLIFT,
        command_type=command_type,
        job_id=delivery.production_job_id,
        job_delivery_id=delivery.job_delivery_id,
        attempt_no=1,
        status=status,
        request_payload_json=json.dumps(payload),
    )
    session.add(attempt)
    session.commit()
    return attempt


def test_empty_pallet_return_monitoring_uses_durable_attempt_and_drop_authority(
    client: TestClient, db_session: Session,
) -> None:
    job, delivery, items = _delivery(
        db_session, suffix="EMPTY_RETURN", status=MaterialDeliveryStatus.COMPLETED
    )

    initial = _body(client, job)[0]
    assert initial["empty_pallet_return_status"] == "NOT_READY"
    assert initial["can_empty_pallet_return"] is False

    for item in items:
        step = db_session.get(JobStep, item.job_step_id)
        assert step is not None
        step.status = StepStatus.COMPLETED
    db_session.commit()
    _transport_attempt(db_session, delivery=delivery)

    eligible = _body(client, job)[0]
    assert eligible["empty_pallet_return_status"] == "ELIGIBLE"
    assert eligible["can_empty_pallet_return"] is True

    active_return = _transport_attempt(
        db_session,
        delivery=delivery,
        command_type="EXECUTE_TRANSPORT_EMPTY_RETURN",
        status=ExecutionAttemptStatus.CREATED,
    )
    in_progress = _body(client, job)[0]
    assert in_progress["empty_pallet_return_status"] == "IN_PROGRESS"
    assert in_progress["can_empty_pallet_return"] is False
    active_return.status = ExecutionAttemptStatus.SUCCEEDED
    db_session.commit()
    returned = _body(client, job)[0]
    refreshed = _body(client, job)[0]
    assert returned["empty_pallet_return_status"] == "SUCCEEDED"
    assert returned["can_empty_pallet_return"] is False
    assert refreshed["empty_pallet_return_status"] == "SUCCEEDED"
    assert refreshed["can_empty_pallet_return"] is False


def test_empty_pallet_return_monitoring_rejects_other_drop_owner_and_terminal_job(
    client: TestClient, db_session: Session,
) -> None:
    owner_job, owner, owner_items = _delivery(
        db_session, suffix="DROP_OWNER", status=MaterialDeliveryStatus.COMPLETED
    )
    for item in owner_items:
        step = db_session.get(JobStep, item.job_step_id)
        assert step is not None
        step.status = StepStatus.COMPLETED
    db_session.commit()
    _transport_attempt(db_session, delivery=owner)

    target_job, target, target_items = _delivery(
        db_session, suffix="DROP_OTHER", status=MaterialDeliveryStatus.COMPLETED
    )
    for item in target_items:
        step = db_session.get(JobStep, item.job_step_id)
        assert step is not None
        step.status = StepStatus.COMPLETED
    db_session.commit()

    blocked_by_owner = _body(client, target_job)[0]
    assert blocked_by_owner["empty_pallet_return_status"] == "NOT_READY"
    assert blocked_by_owner["can_empty_pallet_return"] is False

    target_job.status = JobStatus.CANCELED
    db_session.commit()
    terminal = _body(client, target_job)[0]
    assert terminal["can_empty_pallet_return"] is False


def test_terminal_owner_exposes_test_only_drop_cleanup_state(
    client: TestClient, db_session: Session,
) -> None:
    job, delivery, _ = _delivery(
        db_session, suffix="TERMINAL_OWNER", status=MaterialDeliveryStatus.COMPLETED,
    )
    delivery.supply_group_code = "OUTER_WALLS"
    delivery.supply_destination_code = "DROP"
    _transport_attempt(db_session, delivery=delivery)
    job.status = JobStatus.CANCELED
    db_session.commit()

    response = _body(client, job)[0]
    assert response["empty_pallet_return_status"] == "NOT_READY"
    assert response["terminal_drop_cleanup_required"] is True
    assert response["can_terminal_drop_cleanup"] is True


@pytest.mark.parametrize(
    "status", [MaterialInspectionStatus.REQUESTED, MaterialInspectionStatus.RUNNING]
)
def test_requested_and_running_are_visible_but_not_released(
    client: TestClient, db_session: Session, status: MaterialInspectionStatus,
) -> None:
    job, _, items = _delivery(db_session, suffix=status.value)
    row = _inspection(db_session, items[0], cycle=1, status=status, failure_reason="send retry pending" if status is MaterialInspectionStatus.REQUESTED else None)

    item = _body(client, job)[0]["items"][0]

    assert item["qa_state"] == status.value
    assert item["qa_released"] is False
    assert item["latest_inspection"]["inspection_request_id"] == row.inspection_request_id
    assert item["latest_inspection"]["inspection_cycle"] == 1
    assert item["latest_inspection"]["failure_reason"] == row.failure_reason


@pytest.mark.parametrize(
    ("result", "production_valid", "expected_state", "released"),
    [
        (MaterialInspectionResult.PASS, True, "RELEASED", True),
        (MaterialInspectionResult.FAIL, False, "FAILED", False),
        (MaterialInspectionResult.NOT_EVALUATED, False, "NOT_EVALUATED", False),
        (MaterialInspectionResult.PASS, False, "RELEASED", True),
    ],
)
def test_terminal_qa_summary_uses_authoritative_release_predicate(
    client: TestClient,
    db_session: Session,
    result: MaterialInspectionResult,
    production_valid: bool,
    expected_state: str,
    released: bool,
) -> None:
    job, _, items = _delivery(db_session, suffix=f"TERM_{result}_{production_valid}")
    _inspection(db_session, items[0], cycle=1, status=MaterialInspectionStatus.COMPLETED, result=result, production_valid=production_valid, failure_reason="QA evidence")

    entry = _body(client, job)[0]
    item = entry["items"][0]

    assert item["qa_state"] == expected_state
    assert item["qa_released"] is released
    assert item["latest_inspection"]["production_valid"] is production_valid
    assert entry["qa_released_items"] == int(released)
    assert entry["qa_all_released"] is released


def test_terminal_error_and_latest_cycle_selection_are_truthful(
    client: TestClient, db_session: Session,
) -> None:
    job, _, items = _delivery(db_session, suffix="CYCLES")
    _inspection(db_session, items[0], cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.FAIL, production_valid=False, failure_reason="wrong material")
    latest = _inspection(db_session, items[0], cycle=2, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, production_valid=True)

    item = _body(client, job)[0]["items"][0]
    assert item["qa_state"] == "RELEASED"
    assert item["qa_released"] is True
    assert item["latest_inspection"]["inspection_cycle"] == latest.inspection_cycle

    job2, _, items2 = _delivery(db_session, suffix="LATEST_RUNNING")
    _inspection(db_session, items2[0], cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, production_valid=True)
    latest_running = _inspection(db_session, items2[0], cycle=2, status=MaterialInspectionStatus.RUNNING)
    running_item = _body(client, job2)[0]["items"][0]
    assert running_item["qa_state"] == "RUNNING"
    assert running_item["qa_released"] is False
    assert running_item["latest_inspection"]["inspection_cycle"] == latest_running.inspection_cycle

    job3, _, items3 = _delivery(db_session, suffix="ERROR")
    _inspection(db_session, items3[0], cycle=1, status=MaterialInspectionStatus.ERROR, failure_reason="Vision terminal error")
    error_item = _body(client, job3)[0]["items"][0]
    assert error_item["qa_state"] == "ERROR"
    assert error_item["qa_released"] is False
    assert error_item["latest_inspection"]["failure_reason"] == "Vision terminal error"


def test_multi_group_counts_are_independent_and_physical_ready_is_read_only(
    client: TestClient, db_session: Session,
) -> None:
    job, outer, outer_items = _delivery(db_session, suffix="OUTER", item_count=4, group="OUTER")
    _, inner, inner_items = _delivery(db_session, suffix="INNER", item_count=1, group="INNER", job=job)
    for item in outer_items[:3]:
        _inspection(db_session, item, cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, production_valid=True)
    _inspection(db_session, outer_items[3], cycle=1, status=MaterialInspectionStatus.RUNNING)
    _inspection(db_session, inner_items[0], cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, production_valid=True)

    before_status = outer.status
    body = _body(client, job)
    by_group = {entry["supply_group_code"]: entry for entry in body}

    assert by_group["OUTER"]["qa_total_items"] == 4
    assert by_group["OUTER"]["qa_released_items"] == 3
    assert by_group["OUTER"]["qa_all_released"] is False
    assert by_group["INNER"]["qa_total_items"] == 1
    assert by_group["INNER"]["qa_released_items"] == 1
    assert by_group["INNER"]["qa_all_released"] is True
    db_session.refresh(outer)
    assert outer.status is before_status

    confirmed = client.post(
        f"/production/jobs/{job.job_id}/material-deliveries/{inner.job_delivery_id}/physical-ready",
        json={"request_id": "monitor-ready"},
    )
    assert confirmed.status_code == 200
    inner_read = {entry["supply_group_code"]: entry for entry in _body(client, job)}["INNER"]
    assert inner_read["physical_ready"] is True
    assert inner_read["physical_ready_at"] is not None


@pytest.mark.parametrize("status", list(MaterialDeliveryStatus))
def test_delivery_transport_status_and_manual_legacy_representation(
    client: TestClient, db_session: Session, status: MaterialDeliveryStatus,
) -> None:
    job, _, _ = _delivery(db_session, suffix=f"STATUS_{status.value}", status=status)
    entry = _body(client, job)[0]
    assert entry["status"] == status.value

    manual_job, _, _ = _delivery(db_session, suffix=f"MANUAL_{status.value}", mode=SupplyMode.MANUAL, group="MANUAL")
    manual = _body(client, manual_job)[0]
    assert manual["supply_mode"] == "MANUAL"
    assert manual["qa_applicable"] is True
    assert manual["manual_prestage_ready"] is False
    assert manual["manual_prestage_ready_at"] is None
    assert manual["items"][0]["qa_state"] == "NOT_REQUESTED"

    legacy_job, _, _ = _delivery(db_session, suffix=f"LEGACY_{status.value}", mode=None, group=None)
    legacy = _body(client, legacy_job)[0]
    assert legacy["supply_mode"] is None
    assert legacy["qa_applicable"] is None


def test_outer_wall_batch_monitoring_allows_pending_and_completed_members(
    client: TestClient, db_session: Session,
) -> None:
    job, _, items = _delivery(
        db_session,
        suffix="OUTER_BATCH",
        item_count=2,
        group="OUTER_WALLS",
        status=MaterialDeliveryStatus.COMPLETED,
    )
    for index, item in enumerate(items):
        step = db_session.get(JobStep, item.job_step_id)
        assert step is not None
        step.operation_code = "INSTALL_LEFT_OUTER_WALL"
        step.status = StepStatus.PENDING if index == 0 else StepStatus.COMPLETED
    db_session.commit()

    pending_and_completed = _body(client, job)[0]
    assert pending_and_completed["can_start_outer_wall_batch"] is True

    pending_step = db_session.get(JobStep, items[0].job_step_id)
    assert pending_step is not None
    pending_step.status = StepStatus.COMPLETED
    db_session.commit()
    all_completed = _body(client, job)[0]
    assert all_completed["can_start_outer_wall_batch"] is False
