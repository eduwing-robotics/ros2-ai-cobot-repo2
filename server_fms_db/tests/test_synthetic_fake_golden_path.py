"""Synthetic public-boundary E2E coverage for the policy TRANSPORTED path.

This intentionally seeds only test master/configuration data.  Runtime rows are
created by the same conversation, materialization, operator-command, callback,
and FMS Worker boundaries used by the application.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_conversation_service
from api_server.routers.inventory import get_db
from api_server.routers.production import get_incoming_material_qa_dispatch_coordinator
from api_server.services.production_conversation_service import ProductionConversationService
from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.incoming_material_qa_dispatch_coordinator import IncomingMaterialQADispatchCoordinator
from fms_server.incoming_material_qa_http_client import IncomingMaterialQAAcknowledgement
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    EventType,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    InstallationSlot,
    Inventory,
    JobMaterialDelivery,
    JobMaterialFeedExecution,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    Part,
    PartCategory,
    PendingProductionRequest,
    PendingProductionState,
    Product,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionJob,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)
from shared.schemas.ai import StructuredCommand
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.production_request_materialization_service import ProductionRequestMaterializationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService


class SyntheticGoldenInterpreter:
    """Deterministic text interpretation only; all conversation state stays real."""

    async def interpret(self, text: str):
        return (
            text,
            StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_name="Synthetic Golden Product",
                product_code="TEST_GOLDEN_PRODUCT",
                quantity=1,
                roof_option_code=RoofOptionCode.ROOF_01,
                requires_confirmation=True,
            ),
            "{}",
        )


class RecordingFakeVisionClient:
    def __init__(self) -> None:
        self.requests = []

    def send_request(self, request):
        self.requests.append(request)
        return IncomingMaterialQAAcknowledgement(status_code=202)


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
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


@pytest.fixture(name="vision")
def vision_fixture() -> RecordingFakeVisionClient:
    return RecordingFakeVisionClient()


@pytest.fixture(name="client")
def client_fixture(
    db_session: Session,
    vision: RecordingFakeVisionClient,
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    conversation = ProductionConversationService(
        pending_service=PendingProductionRequestService(db_session),
        interpreter=SyntheticGoldenInterpreter(),
        materialization_service=ProductionRequestMaterializationService(db_session),
        preflight_service=ProductionInventoryPreflightService(db_session),
    )
    qa_coordinator = IncomingMaterialQADispatchCoordinator(
        db_session,
        runtime=IncomingMaterialQARuntime(client=vision),
    )
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_conversation_service] = lambda: conversation
    app.dependency_overrides[get_incoming_material_qa_dispatch_coordinator] = lambda: qa_coordinator
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _seed_synthetic_master(session: Session) -> None:
    """Seed only fixture master/config rows; never runtime production rows."""
    product = Product(
        product_code="TEST_GOLDEN_PRODUCT",
        product_name="Synthetic Golden Product",
    )
    part = Part(
        part_code="TEST_GOLDEN_PART_01",
        part_name="Synthetic transported wall",
        category=PartCategory.STRUCTURE,
        vision_class="wall_ext_left",
        unit="EA",
    )
    session.add_all((product, part))
    session.flush()
    session.add_all(
        (
            InstallationSlot(
                product_code=product.product_code,
                slot_code="TEST_GOLDEN_SLOT_01",
                display_name="Synthetic Golden Slot",
            ),
            Inventory(part_code=part.part_code, quantity=10),
        )
    )
    recipe = AssemblyRecipe(
        product_code=product.product_code,
        version=1,
        is_active=True,
        description="Synthetic fake golden-path recipe only",
    )
    session.add(recipe)
    session.flush()
    session.add(
        AssemblyRecipeStage(
            recipe_id=recipe.recipe_id,
            stage_order=1,
            operation_code="INSTALL_LEFT_OUTER_WALL",
            display_name="Synthetic transported install",
            part_code=part.part_code,
            quantity=1,
            slot_code="TEST_GOLDEN_SLOT_01",
            pick_zone="TEST_GOLDEN_PICK_ZONE_01",
            supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="OUTER_WALLS",
            supply_destination_code="DROP",
            is_terminal=True,
        )
    )
    session.commit()


def _worker(
    session_factory: sessionmaker[Session],
) -> tuple[FmsWorker, FakeForkliftActionTransport, FakeCellActionTransport]:
    forklift_transport = FakeForkliftActionTransport()
    cell_transport = FakeCellActionTransport(
        [FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(4)]
    )
    forklift_adapter = ForkliftActionAdapter(forklift_transport)
    cell_adapter = RobotCellActionAdapter(cell_transport)

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

    return (
        FmsWorker(
            session_factory=session_factory,
            fms_execution_coordinator_factory=fms_factory,
            forklift_execution_coordinator_factory=forklift_factory,
            material_feed_execution_coordinator_factory=feed_factory,
        ),
        forklift_transport,
        cell_transport,
    )


def _materialize_one_job(client: TestClient, session: Session, *, session_id: str) -> tuple[ProductionJob, JobStep, JobMaterialDelivery, JobMaterialDeliveryItem]:
    jobs_before = session.scalar(select(func.count()).select_from(ProductionJob))
    first = client.post(
        "/ai/conversation",
        json={"session_id": session_id, "text": "synthetic product one unit please"},
    )
    assert first.status_code == 200
    assert first.json()["conversation_state"] == PendingProductionState.AWAITING_CONFIRMATION.value
    assert first.json()["production_job_ids"] == []
    pending_id = first.json()["pending_request_id"]
    assert pending_id is not None
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before
    pending = session.get(PendingProductionRequest, pending_id)
    assert pending is not None and pending.state is PendingProductionState.AWAITING_CONFIRMATION

    confirmed = client.post("/ai/conversation", json={"session_id": session_id, "text": "네"})
    assert confirmed.status_code == 200
    assert confirmed.json()["conversation_state"] == PendingProductionState.CONFIRMED.value
    assert len(confirmed.json()["production_job_ids"]) == 1
    job = session.get(ProductionJob, confirmed.json()["production_job_ids"][0])
    assert job is not None
    step = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id))
    delivery = session.scalar(select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id))
    assert step is not None and delivery is not None
    item = session.scalar(select(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id == delivery.job_delivery_id))
    assert item is not None
    return job, step, delivery, item


def _qa_start_url(job: ProductionJob, delivery: JobMaterialDelivery, item: JobMaterialDeliveryItem) -> str:
    return (
        f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}"
        f"/items/{item.delivery_item_id}/incoming-qa/start"
    )


def _physical_ready_url(job: ProductionJob, delivery: JobMaterialDelivery) -> str:
    return f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}/physical-ready"


def _pass_callback_payload(item: JobMaterialDeliveryItem, request_id: str, cycle: int) -> dict[str, object]:
    return {
        "ver": "0.1",
        "inspection_request_id": request_id,
        "delivery_item_id": item.delivery_item_id,
        "inspection_cycle": cycle,
        "status": "COMPLETED",
        "result": "PASS",
        "failure_type": None,
        "expected_part_code": item.part_code,
        "expected_class_name": "wall_ext_left",
        "expected_quantity": item.quantity,
        "detected_quantity": item.quantity,
        "detections": [],
        "frame_width": 640,
        "frame_height": 480,
        "camera_source": "D435",
        "frame_seq": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_scope": "synthetic-test",
        "model_version": "1",
        "production_valid": True,
    }


def _snapshot_reason(client: TestClient, job_id: int) -> str | None:
    response = client.get(f"/production/jobs/{job_id}/execution-snapshot")
    assert response.status_code == 200
    next_step = response.json()["next_step"]
    return next_step["readiness_reason"] if next_step is not None else None


def test_synthetic_fake_golden_path_uses_public_boundaries_end_to_end(
    client: TestClient,
    db_session: Session,
    session_factory: sessionmaker[Session],
    vision: RecordingFakeVisionClient,
) -> None:
    _seed_synthetic_master(db_session)
    job, step, delivery, item = _materialize_one_job(client, db_session, session_id="synthetic-golden-main")

    assert job.status is JobStatus.REQUESTED
    assert (step.part_code, step.vision_class, step.quantity, step.slot_code, step.pick_zone) == (
        "TEST_GOLDEN_PART_01",
        "wall_ext_left",
        1,
        "TEST_GOLDEN_SLOT_01",
        "TEST_GOLDEN_PICK_ZONE_01",
    )
    assert (step.supply_mode, step.supply_group_code, step.supply_destination_code) == (
        SupplyMode.TRANSPORTED,
        "OUTER_WALLS",
        "DROP",
    )
    assert (delivery.supply_mode, delivery.supply_group_code, delivery.supply_destination_code) == (
        step.supply_mode,
        step.supply_group_code,
        step.supply_destination_code,
    )
    assert (item.job_step_id, item.part_code, item.quantity) == (step.job_step_id, step.part_code, step.quantity)
    assert db_session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == 0
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 0
    assert db_session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id == job.job_id, ProductionEvent.event_type == EventType.JOB_CREATED)) == 1

    initial_monitoring = client.get(f"/production/jobs/{job.job_id}/material-deliveries")
    assert initial_monitoring.status_code == 200
    assert initial_monitoring.json()[0]["items"][0]["qa_state"] == "NOT_REQUESTED"
    assert initial_monitoring.json()[0]["physical_ready"] is False

    worker, forklift, cell = _worker(session_factory)
    # First regular tick starts the Job, but neither external action is eligible.
    assert worker.tick() is True
    db_session.refresh(job)
    assert job.status is JobStatus.RUNNING
    assert _snapshot_reason(client, job.job_id) == StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE.value
    assert forklift.execute_transport_requests == []
    assert cell.commands == []
    assert db_session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0

    started = client.post(_qa_start_url(job, delivery, item))
    assert started.status_code == 200
    assert started.json()["action"] == "CREATE_NEW"
    assert started.json()["inspection_cycle"] == 1
    assert started.json()["vision_request_sent"] is True
    assert len(vision.requests) == 1
    inspection = db_session.scalar(select(MaterialInspection).where(MaterialInspection.delivery_item_id == item.delivery_item_id))
    assert inspection is not None
    assert inspection.inspection_request_id == vision.requests[0].inspection_request_id
    assert inspection.status.value == "RUNNING"
    running_monitoring = client.get(f"/production/jobs/{job.job_id}/material-deliveries").json()[0]
    assert running_monitoring["items"][0]["qa_state"] == "RUNNING"
    assert forklift.execute_transport_requests == []
    assert cell.commands == []

    # Historical Fake fixture seam: production no longer exposes a public
    # result injector. The v0.2 UDP runtime remains the real public authority.
    applied = MaterialInspectionService().apply_inspection_result(
        db_session,
        IncomingMaterialQAResult.model_validate(
            _pass_callback_payload(item, inspection.inspection_request_id, inspection.inspection_cycle)
        ),
    )
    db_session.commit()
    assert MaterialInspectionService.is_release_allowed(applied) is True
    db_session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.PENDING
    assert delivery.physical_ready_at is None
    released_monitoring = client.get(f"/production/jobs/{job.job_id}/material-deliveries").json()[0]
    assert released_monitoring["items"][0]["qa_state"] == "RELEASED"
    assert released_monitoring["qa_all_released"] is True
    assert _snapshot_reason(client, job.job_id) == StepReadinessReason.PHYSICAL_READY_REQUIRED.value
    assert worker.tick() is False
    assert forklift.execute_transport_requests == []
    assert cell.commands == []

    ready = client.post(_physical_ready_url(job, delivery), json={"request_id": "synthetic-golden-ready-1"})
    assert ready.status_code == 200
    db_session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.PENDING
    assert delivery.physical_ready_at is not None
    assert delivery.physical_ready_request_id == "synthetic-golden-ready-1"
    assert db_session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
    assert _snapshot_reason(client, job.job_id) == StepReadinessReason.TRANSPORT_PENDING.value

    # The normal Worker owns the atomic claim, fake transport, then its completion.
    assert worker.tick() is True
    db_session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    assert len(forklift.execute_transport_requests) == 1
    assert cell.commands == []
    assert db_session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == 0
    transport_attempt = db_session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
            ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
        )
    )
    assert transport_attempt is not None and transport_attempt.status is ExecutionAttemptStatus.SUCCEEDED
    readiness = StepReadinessService(MaterialDeliveryService(db_session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id
    )
    assert readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
    release = client.post(
        f"/production/jobs/{job.job_id}/steps/{step.job_step_id}/execution-ready"
    )
    assert release.status_code == 200, release.text
    assert StepReadinessService(MaterialDeliveryService(db_session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id
    ).ready is True

    # The following ordinary tick dispatches the existing Robot Cell coordinator.
    assert worker.tick() is True
    db_session.refresh(step)
    db_session.refresh(job)
    assert step.status is StepStatus.COMPLETED
    assert len(cell.commands) == 1
    assert cell.commands[0].task_type == "INSTALL_OUTER_WALL"
    assert job.status is JobStatus.COMPLETED
    pre_roof = db_session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert pre_roof is None
    cell_attempt = db_session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.job_step_id == step.job_step_id,
            ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
        )
    )
    assert cell_attempt is not None and cell_attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert [event.event_type for event in db_session.scalars(
        select(ProductionEvent).where(ProductionEvent.job_id == job.job_id).order_by(ProductionEvent.event_id)
    )] == [
        EventType.JOB_CREATED,
        EventType.JOB_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
        EventType.JOB_COMPLETED,
    ]
    completed_monitoring = client.get(f"/production/jobs/{job.job_id}/material-deliveries").json()[0]
    assert completed_monitoring["status"] == MaterialDeliveryStatus.COMPLETED.value
    assert completed_monitoring["physical_ready"] is True
    assert completed_monitoring["items"][0]["qa_state"] == "RELEASED"

    attempts_before = db_session.scalar(select(func.count()).select_from(ExecutionAttempt))
    assert worker.tick() is False
    assert len(forklift.execute_transport_requests) == 1
    assert len(cell.commands) == 1
    assert db_session.scalar(select(func.count()).select_from(ExecutionAttempt)) == attempts_before


def test_synthetic_transported_worker_requires_both_qa_release_and_physical_ready(
    client: TestClient,
    db_session: Session,
    session_factory: sessionmaker[Session],
    vision: RecordingFakeVisionClient,
) -> None:
    _seed_synthetic_master(db_session)
    worker, forklift, cell = _worker(session_factory)

    # Ready without QA is rejected at the operator endpoint and cannot dispatch transport.
    no_qa_job, _, no_qa_delivery, _ = _materialize_one_job(client, db_session, session_id="synthetic-golden-ready-only")
    assert worker.tick() is True  # normal Job start only
    physical = client.post(_physical_ready_url(no_qa_job, no_qa_delivery), json={"request_id": "synthetic-ready-only"})
    assert physical.status_code == 409
    assert "Incoming QA RELEASE is required" in physical.json()["detail"]
    db_session.refresh(no_qa_delivery)
    assert no_qa_delivery.physical_ready_at is None
    assert worker.tick() is False
    assert forklift.execute_transport_requests == []
    assert cell.commands == []

    # QA release without physical-ready also cannot dispatch transport.
    qa_job, _, qa_delivery, qa_item = _materialize_one_job(client, db_session, session_id="synthetic-golden-qa-only")
    assert worker.tick() is True  # starts only the second Job
    started = client.post(_qa_start_url(qa_job, qa_delivery, qa_item))
    assert started.status_code == 200
    inspection = db_session.scalar(select(MaterialInspection).where(MaterialInspection.delivery_item_id == qa_item.delivery_item_id))
    assert inspection is not None
    applied = MaterialInspectionService().apply_inspection_result(
        db_session,
        IncomingMaterialQAResult.model_validate(
            _pass_callback_payload(qa_item, inspection.inspection_request_id, inspection.inspection_cycle)
        ),
    )
    db_session.commit()
    assert MaterialInspectionService.is_release_allowed(applied) is True
    assert worker.tick() is False
    assert forklift.execute_transport_requests == []
    assert cell.commands == []
    assert len(vision.requests) == 1
