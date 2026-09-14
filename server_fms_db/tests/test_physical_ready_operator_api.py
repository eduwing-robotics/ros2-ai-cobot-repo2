from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="db_session")
def db_session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _context(
    session: Session,
    *,
    suffix: str,
    mode: SupplyMode | None = SupplyMode.TRANSPORTED,
    delivery_status: MaterialDeliveryStatus = MaterialDeliveryStatus.PENDING,
    qa_passed: bool = False,
) -> tuple[ProductionJob, JobStep, JobMaterialDelivery]:
    product = Product(product_code=f"READY_API_PRODUCT_{suffix}", product_name="Ready API product")
    part = Part(
        part_code=f"READY_API_PART_{suffix}",
        part_name="Ready API part",
        category=PartCategory.STRUCTURE,
        vision_class="wall_ext_left",
        unit="EA",
    )
    session.add_all((product, part))
    session.flush()
    job = ProductionJob(job_code=f"READY_API_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    group = "OUTER_WALLS" if mode is SupplyMode.TRANSPORTED else (f"READY_API_GROUP_{suffix}" if mode is not None else None)
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_OUTER_WALL",
        display_name="Install wall",
        part_code=part.part_code,
        quantity=1,
        slot_code=f"READY_API_SLOT_{suffix}",
        pick_zone="READY_API_ZONE",
        vision_class=part.vision_class,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED else None,
        status=StepStatus.PENDING,
    )
    session.add(step)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=f"READY_API_DEL_{suffix}",
        display_name="Ready API delivery",
        status=delivery_status,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED else None,
    )
    session.add(delivery)
    session.flush()
    item = JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id,
        job_step_id=step.job_step_id,
        part_code=part.part_code,
        quantity=1,
    )
    session.add(item)
    session.flush()
    if qa_passed:
        session.add(
            MaterialInspection(
                inspection_request_id=f"ready-api-qa-{suffix}",
                delivery_item_id=item.delivery_item_id,
                inspection_cycle=1,
                status=MaterialInspectionStatus.COMPLETED,
                result=MaterialInspectionResult.PASS,
                expected_part_code=part.part_code,
                expected_class_name=part.vision_class,
                expected_quantity=1,
                detected_quantity=1,
                production_valid=True,
                completed_at=datetime.now(timezone.utc),
            )
        )
    session.commit()
    return job, step, delivery


def _url(job: ProductionJob, delivery: JobMaterialDelivery) -> str:
    return f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}/physical-ready"


def _manual_prestage_url(job: ProductionJob, delivery: JobMaterialDelivery) -> str:
    return f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}/manual-prestage-ready"


def _worker(session_factory: sessionmaker[Session], forklift: FakeForkliftActionTransport) -> FmsWorker:
    cell_transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(forklift)

    def fms_factory(db: Session) -> FmsExecutionCoordinator:
        return FmsExecutionCoordinator(
            db,
            orchestration_service=ProductionOrchestrationService(db),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(db)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(db),
        )

    def forklift_factory(db: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(
            db,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(db),
        )

    def feed_factory(db: Session) -> MaterialFeedExecutionCoordinator:
        return MaterialFeedExecutionCoordinator(db, robot_cell_adapter=cell_adapter)

    return FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_factory,
        forklift_execution_coordinator_factory=forklift_factory,
        material_feed_execution_coordinator_factory=feed_factory,
    )


def test_operator_command_persists_and_read_model_exposes_original_evidence(
    client: TestClient, db_session: Session,
) -> None:
    job, _, delivery = _context(db_session, suffix="IDEMPOTENT", qa_passed=True)
    first = client.post(_url(job, delivery), json={"request_id": "operator-ready-1"})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["job_id"] == job.job_id
    assert first_body["supply_mode"] == "TRANSPORTED"
    assert first_body["physical_ready_request_id"] == "operator-ready-1"

    same = client.post(_url(job, delivery), json={"request_id": "operator-ready-1"})
    other = client.post(_url(job, delivery), json={"request_id": "operator-ready-2"})
    assert same.status_code == other.status_code == 200
    assert same.json()["physical_ready_at"] == first_body["physical_ready_at"]
    assert other.json()["physical_ready_at"] == first_body["physical_ready_at"]
    assert other.json()["physical_ready_request_id"] == "operator-ready-1"

    read = client.get(f"/production/jobs/{job.job_id}/material-deliveries")
    assert read.status_code == 200
    listed = read.json()[0]
    assert listed["supply_group_code"] == delivery.supply_group_code
    assert listed["physical_ready_at"] == first_body["physical_ready_at"]
    assert listed["physical_ready_request_id"] == "operator-ready-1"


def test_operator_command_preserves_state_and_rejects_invalid_ownership_or_policy(
    client: TestClient, db_session: Session,
) -> None:
    job, step, delivery = _context(db_session, suffix="SIDE_EFFECT", qa_passed=True)
    before_inspections = db_session.scalar(select(func.count()).select_from(MaterialInspection))
    before_attempts = db_session.scalar(select(func.count()).select_from(ExecutionAttempt))
    before_feeds = db_session.scalar(select(func.count()).select_from(JobMaterialFeedExecution))
    response = client.post(_url(job, delivery), json={"request_id": "side-effect-ready"})
    assert response.status_code == 200
    db_session.refresh(delivery)
    db_session.refresh(step)
    assert delivery.status is MaterialDeliveryStatus.PENDING
    assert step.status is StepStatus.PENDING
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == before_inspections
    assert db_session.scalar(select(func.count()).select_from(ExecutionAttempt)) == before_attempts
    assert db_session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == before_feeds

    other_job, _, other_delivery = _context(db_session, suffix="OTHER")
    assert client.post(_url(job, other_delivery), json={"request_id": "wrong-owner"}).status_code == 404
    assert client.post(f"/production/jobs/999999/material-deliveries/{delivery.job_delivery_id}/physical-ready", json={"request_id": "missing-job"}).status_code == 404
    assert other_job.job_id != job.job_id

    legacy_job, _, legacy_delivery = _context(db_session, suffix="LEGACY", mode=None)
    assert client.post(_url(legacy_job, legacy_delivery), json={"request_id": "legacy-ready"}).status_code == 409


@pytest.mark.parametrize("status", [MaterialDeliveryStatus.IN_PROGRESS, MaterialDeliveryStatus.COMPLETED, MaterialDeliveryStatus.FAILED])
def test_operator_command_rejects_first_confirmation_after_transport_started(
    client: TestClient, db_session: Session, status: MaterialDeliveryStatus,
) -> None:
    job, _, delivery = _context(db_session, suffix=f"STATE_{status.value}", delivery_status=status)
    response = client.post(_url(job, delivery), json={"request_id": "late-first-ready"})
    assert response.status_code == 409
    db_session.refresh(delivery)
    assert delivery.physical_ready_at is None


def test_manual_cannot_use_transported_physical_ready_evidence(
    client: TestClient, db_session: Session,
) -> None:
    job, _, delivery = _context(db_session, suffix="MANUAL", mode=SupplyMode.MANUAL)
    response = client.post(_url(job, delivery), json={"request_id": "manual-ready"})
    assert response.status_code == 409
    db_session.refresh(delivery)
    assert delivery.physical_ready_at is None
    assert delivery.manual_prestage_ready_at is None
    assert delivery.status is MaterialDeliveryStatus.PENDING


def test_manual_prestage_command_is_idempotent_and_enables_only_released_manual_step(
    client: TestClient, db_session: Session,
) -> None:
    job, step, delivery = _context(
        db_session,
        suffix="MANUAL_PRESTAGE",
        mode=SupplyMode.MANUAL,
        qa_passed=True,
    )
    first = client.post(_manual_prestage_url(job, delivery), json={"request_id": "manual-prestage-1"})
    assert first.status_code == 200
    body = first.json()
    assert body["supply_mode"] == "MANUAL"
    assert body["manual_prestage_request_id"] == "manual-prestage-1"

    same = client.post(_manual_prestage_url(job, delivery), json={"request_id": "manual-prestage-1"})
    other = client.post(_manual_prestage_url(job, delivery), json={"request_id": "manual-prestage-2"})
    assert same.status_code == other.status_code == 200
    assert same.json()["manual_prestage_ready_at"] == body["manual_prestage_ready_at"]
    assert other.json()["manual_prestage_request_id"] == "manual-prestage-1"
    assert StepReadinessService(MaterialDeliveryService(db_session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id,
    ).ready is True

    read = client.get(f"/production/jobs/{job.job_id}/material-deliveries")
    assert read.status_code == 200
    assert read.json()[0]["manual_prestage_ready"] is True
    assert read.json()[0]["manual_prestage_ready_at"] == body["manual_prestage_ready_at"]
    db_session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.PENDING
    assert delivery.physical_ready_at is None


def test_manual_prestage_never_bypasses_unreleased_or_failed_qa(
    client: TestClient, db_session: Session,
) -> None:
    job, step, delivery = _context(db_session, suffix="MANUAL_QA_HOLD", mode=SupplyMode.MANUAL)
    held = client.post(
        _manual_prestage_url(job, delivery), json={"request_id": "manual-prestage-hold"}
    )
    assert held.status_code == 409
    assert "Incoming QA RELEASE is required" in held.json()["detail"]
    db_session.refresh(delivery)
    assert delivery.manual_prestage_ready_at is None
    readiness = StepReadinessService(MaterialDeliveryService(db_session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id,
    )
    assert readiness.reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE

    item = delivery.items[0]
    db_session.add(MaterialInspection(
        inspection_request_id="manual-qa-fail",
        delivery_item_id=item.delivery_item_id,
        inspection_cycle=1,
        status=MaterialInspectionStatus.COMPLETED,
        result=MaterialInspectionResult.FAIL,
        expected_part_code=item.part_code,
        expected_class_name="wall_ext_left",
        expected_quantity=1,
        detected_quantity=1,
        production_valid=False,
        completed_at=datetime.now(timezone.utc),
    ))
    db_session.commit()
    assert StepReadinessService(MaterialDeliveryService(db_session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id,
    ).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE


def test_manual_prestage_rejects_transport_legacy_wrong_owner_and_late_first_confirmation(
    client: TestClient, db_session: Session,
) -> None:
    transported_job, _, transported_delivery = _context(db_session, suffix="PRESTAGE_TRANSPORT", mode=SupplyMode.TRANSPORTED)
    legacy_job, _, legacy_delivery = _context(db_session, suffix="PRESTAGE_LEGACY", mode=None)
    owner_job, _, owner_delivery = _context(db_session, suffix="PRESTAGE_OWNER", mode=SupplyMode.MANUAL)
    other_job, _, _ = _context(db_session, suffix="PRESTAGE_OTHER", mode=SupplyMode.MANUAL)
    assert client.post(_manual_prestage_url(transported_job, transported_delivery), json={"request_id": "wrong-mode"}).status_code == 409
    assert client.post(_manual_prestage_url(legacy_job, legacy_delivery), json={"request_id": "legacy"}).status_code == 409
    assert client.post(_manual_prestage_url(other_job, owner_delivery), json={"request_id": "wrong-owner"}).status_code == 404
    assert client.post(f"/production/jobs/999999/material-deliveries/{owner_delivery.job_delivery_id}/manual-prestage-ready", json={"request_id": "missing-job"}).status_code == 404

    for status in (MaterialDeliveryStatus.IN_PROGRESS, MaterialDeliveryStatus.COMPLETED, MaterialDeliveryStatus.FAILED):
        late_job, _, late_delivery = _context(
            db_session,
            suffix=f"PRESTAGE_LATE_{status.value}",
            mode=SupplyMode.MANUAL,
            delivery_status=status,
        )
        assert client.post(_manual_prestage_url(late_job, late_delivery), json={"request_id": "late"}).status_code == 409
        db_session.refresh(late_delivery)
        assert late_delivery.manual_prestage_ready_at is None


def test_command_updates_derived_readiness_then_worker_consumes_it_on_next_tick(
    client: TestClient, db_session: Session, session_factory: sessionmaker[Session],
) -> None:
    job, _, delivery = _context(db_session, suffix="WORKER", qa_passed=True)
    before = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert before.status_code == 200
    assert before.json()["next_step"]["readiness_reason"] == "PHYSICAL_READY_REQUIRED"

    forklift = FakeForkliftActionTransport()
    worker = _worker(session_factory, forklift)
    assert worker.tick() is False
    assert len(forklift.execute_transport_requests) == 0

    response = client.post(_url(job, delivery), json={"request_id": "worker-ready"})
    assert response.status_code == 200
    after = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert after.status_code == 200
    assert after.json()["next_step"]["readiness_reason"] == "TRANSPORT_PENDING"
    assert len(forklift.execute_transport_requests) == 0

    assert worker.tick() is True
    assert len(forklift.execute_transport_requests) == 1
    db_session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
