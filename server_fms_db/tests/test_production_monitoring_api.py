from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.services.production_snapshot_service import ProductionSnapshotService
from api_server.routers.inventory import get_db
from shared.models import Base
from tests.recipe_test_support import add_active_recipe, add_gated_roof_stages, seed_complete_incoming_qa, seed_inventory_for_recipe
from shared.models.factory import (
    AssemblyRecipe, AssemblyRecipeStage, EventType, JobMaterialDelivery, JobStatus, JobStep,
    Part, PartCategory, Product, ProductionEvent, ProductionInspection, ProductionInspectionStatus, ProductionJob, ProductionJobControlState, RoofOptionCode, StepStatus,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.schemas.vision import IncomingMaterialQAResult

PROCESS_STEP_CODES = [f"TEST_OPERATION_{index:02d}" for index in range(1, 17)]


@pytest.fixture(name="db_session")
def fixture_db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

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
def fixture_client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def seed_product_and_steps(session: Session) -> Product:
    product = Product(product_code="HOUSE_MONITOR", product_name="Monitoring Test House")
    session.add(product)
    session.flush()
    recipe = add_active_recipe(session, product.product_code)
    add_gated_roof_stages(session, recipe)
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return product


def create_job(session: Session, *, job_code: str = "MONITOR-001"):
    product = seed_product_and_steps(session)
    job = ProductionOrchestrationService(session).create_job(
        product_code=product.product_code,
        job_code=job_code,
        roof_option_code=RoofOptionCode.ROOF_01,
    )
    seed_complete_incoming_qa(session, job_id=job.job_id)
    return job


def _assert_rest_unity_readiness_parity(
    client: TestClient, db_session: Session, job_id: int, *, expected_ready: bool, expected_reason: str | None
) -> None:
    rest = client.get(f"/production/jobs/{job_id}/execution-snapshot")
    assert rest.status_code == 200
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    unity_status = ProductionSnapshotService(factory).get_job_status(job_id)
    assert rest.json()["current_step"] == unity_status["current_step"]
    assert rest.json()["next_step"]["operation_code"] == unity_status["next_step"]["operation_code"]
    assert rest.json()["next_step"]["ready"] is expected_ready
    assert unity_status["next_step"]["ready"] is expected_ready
    assert rest.json()["next_step"]["readiness_reason"] == expected_reason
    assert unity_status["next_step"]["readiness_reason"] == expected_reason


def test_get_production_jobs_returns_job_summaries(client: TestClient, db_session: Session) -> None:
    job = create_job(db_session)

    response = client.get("/production/jobs")

    assert response.status_code == 200
    assert response.json() == [
        {
            "job_id": job.job_id,
            "job_code": "MONITOR-001",
            "product_code": job.product_code,
            "roof_option_code": "ROOF_01",
            "assembly_recipe_id": job.assembly_recipe_id,
            "assembly_recipe_version": 1,
            "source_pending_request_id": None,
            "source_item_index": None,
            "status": "REQUESTED",
            "control_state": "ACTIVE",
            "requested_at": response.json()[0]["requested_at"],
            "started_at": None,
            "completed_at": None,
        }
    ]



def test_production_job_read_endpoints_return_roof_option_code(
    client: TestClient,
    db_session: Session,
) -> None:
    product = seed_product_and_steps(db_session)
    job = ProductionOrchestrationService(db_session).create_job(
        product_code=product.product_code,
        job_code="MONITOR-ROOF-001",
        roof_option_code=RoofOptionCode.ROOF_02,
    )

    list_response = client.get("/production/jobs")
    detail_response = client.get(f"/production/jobs/{job.job_id}")

    assert list_response.status_code == 200
    assert list_response.json()[0]["roof_option_code"] == "ROOF_02"
    assert detail_response.status_code == 200
    assert detail_response.json()["roof_option_code"] == "ROOF_02"

def test_get_production_job_returns_s1_to_s9_and_running_current_step(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session)
    service = ProductionOrchestrationService(db_session)
    service.start_job(job.job_id)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None
    service.start_step(first_step.job_step_id)

    response = client.get(f"/production/jobs/{job.job_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "RUNNING"
    assert [step["step_code"] for step in data["steps"]] == PROCESS_STEP_CODES
    assert [step["step_order"] for step in data["steps"]] == list(range(1, 17))
    assert data["current_step"]["job_step_id"] == first_step.job_step_id
    assert data["current_step"]["step_code"] == "TEST_OPERATION_01"
    assert data["current_step"]["status"] == "RUNNING"
    assert {"process_stage_code", "process_stage_order", "process_stage_display_name"} <= data.keys()


def test_requested_job_has_no_current_step(client: TestClient, db_session: Session) -> None:
    job = create_job(db_session)

    response = client.get(f"/production/jobs/{job.job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"
    assert response.json()["current_step"] is None


def test_failed_job_has_no_current_step_even_when_later_steps_are_pending(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session)
    service = ProductionOrchestrationService(db_session)
    service.start_job(job.job_id)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None
    service.start_step(first_step.job_step_id)
    service.fail_step(first_step.job_step_id, reason="robot cell failed")

    response = client.get(f"/production/jobs/{job.job_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "FAILED"
    assert data["steps"][0]["status"] == "FAILED"
    assert data["steps"][1]["status"] == "PENDING"
    assert data["current_step"] is None


def test_completed_job_has_no_current_step(client: TestClient, db_session: Session) -> None:
    job = create_job(db_session)
    service = ProductionOrchestrationService(db_session)
    service.start_job(job.job_id)
    for _ in PROCESS_STEP_CODES:
        step = service.get_next_step(job.job_id)
        assert step is not None
        service.start_step(step.job_step_id)
        service.complete_step(step.job_step_id)

    response = client.get(f"/production/jobs/{job.job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "PRE_ROOF_READY"
    assert response.json()["current_step"] is None


def test_runtime_roof_step_read_endpoint_exposes_job_step_snapshot(
    client: TestClient,
    db_session: Session,
) -> None:
    product = seed_product_and_steps(db_session)
    job = ProductionOrchestrationService(db_session).create_job(
        product_code=product.product_code,
        job_code="MONITOR-RUNTIME-ROOF",
        roof_option_code=RoofOptionCode.ROOF_02,
    )
    seed_complete_incoming_qa(db_session, job_id=job.job_id)
    orchestration = ProductionOrchestrationService(db_session)
    orchestration.start_job(job.job_id)
    for _ in PROCESS_STEP_CODES:
        step = orchestration.get_next_step(job.job_id)
        assert step is not None
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)

    inspection = client.get(f"/production/jobs/{job.job_id}/inspection")
    assert inspection.status_code == 200
    assert inspection.json()["status"] == "PENDING"
    assert client.get(f"/production/jobs/{job.job_id}/roof-step").json() is None

    lifecycle = ProductionCompletionService(db_session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = client.get(f"/production/jobs/{job.job_id}/roof-step")
    assert roof_step.status_code == 200
    assert roof_step.json()["operation_code"] == "INSTALL_ROOF"
    assert roof_step.json()["status"] == "PENDING"



def test_get_production_job_events_returns_chronological_history(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session)
    service = ProductionOrchestrationService(db_session)
    service.start_job(job.job_id)
    step = service.get_next_step(job.job_id)
    assert step is not None
    service.start_step(step.job_step_id)
    service.complete_step(step.job_step_id)

    response = client.get(f"/production/jobs/{job.job_id}/events")

    assert response.status_code == 200
    events = response.json()
    assert [event["event_type"] for event in events] == [
        "JOB_CREATED",
        "JOB_STARTED",
        "STEP_STARTED",
        "STEP_COMPLETED",
    ]
    assert events[2]["job_step_id"] == step.job_step_id
    assert events[0]["message"]
    assert events[0]["created_at"]





def test_material_delivery_read_endpoint_returns_configured_delivery_for_materialized_job(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session, job_code="MONITOR-NO-DELIVERY")

    response = client.get(f"/production/jobs/{job.job_id}/material-deliveries")

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_execution_snapshot_is_read_only_and_reports_pending_step_contract_gap(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session, job_code="MONITOR-SNAPSHOT-PENDING")
    orchestration = ProductionOrchestrationService(db_session)
    orchestration.start_job(job.job_id)
    event_count_before = db_session.query(ProductionEvent).filter(ProductionEvent.job_id == job.job_id).count()

    response = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["job_status"] == "RUNNING"
    assert body["control_state"] == "ACTIVE"
    assert body["current_step"] is None
    assert body["next_step"]["operation_code"] == PROCESS_STEP_CODES[0]
    assert body["next_step"]["ready"] is True
    assert body["next_step"]["readiness_reason"] is None
    # A monitor does not fabricate caller-owned opaque parts_json.
    assert body["next_step"]["dispatchable"] is False
    assert body["next_step"]["dispatch_block_reason"] == "UNSUPPORTED_OPERATION_CODE"
    assert db_session.get(ProductionJob, job.job_id).status is JobStatus.RUNNING
    assert db_session.query(ProductionEvent).filter(ProductionEvent.job_id == job.job_id).count() == event_count_before

    job.control_state = ProductionJobControlState.PAUSED
    db_session.commit()
    paused = client.get("/production/jobs/%s/execution-snapshot" % job.job_id).json()
    assert paused["job_status"] == "RUNNING"
    assert paused["control_state"] == "PAUSED"
    assert paused["next_step"]["dispatchable"] is False
    assert paused["next_step"]["dispatch_block_reason"] == "JOB_CONTROL_PAUSED"


def test_execution_snapshot_reports_running_step_without_reoffering_it(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session, job_code="MONITOR-SNAPSHOT-RUNNING")
    orchestration = ProductionOrchestrationService(db_session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None
    orchestration.start_step(step.job_step_id)

    response = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["current_step"]["job_step_id"] == step.job_step_id
    assert body["current_step"]["status"] == "RUNNING"
    assert body["next_step"] is None


def test_execution_snapshot_reports_material_not_ready_without_dispatch(
    client: TestClient,
    db_session: Session,
) -> None:
    product = seed_product_and_steps(db_session)
    recipe = db_session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == product.product_code))
    assert recipe is not None
    part = Part(vision_class="wall_ext_back", part_code="TEST_PART", part_name="Test Part", category=PartCategory.STRUCTURE, unit="EA")
    db_session.add(part)
    db_session.flush()
    stage = db_session.scalar(select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id).order_by(AssemblyRecipeStage.stage_order))
    stage.part_code = part.part_code
    stage.quantity = 1
    seed_inventory_for_recipe(db_session, recipe)
    db_session.flush()
    job = ProductionOrchestrationService(db_session).create_job(
        product_code=product.product_code,
        job_code="MONITOR-SNAPSHOT-MATERIAL",
        roof_option_code=RoofOptionCode.ROOF_01,
    )
    seed_complete_incoming_qa(db_session, job_id=job.job_id)
    ProductionOrchestrationService(db_session).start_job(job.job_id)
    delivery = db_session.query(JobMaterialDelivery).filter(
        JobMaterialDelivery.production_job_id == job.job_id
    ).order_by(JobMaterialDelivery.job_delivery_id).first()
    assert delivery is not None

    delivery_response = client.get(f"/production/jobs/{job.job_id}/material-deliveries")
    assert delivery_response.status_code == 200
    feed_payload = delivery_response.json()[0]["feed_execution"]
    assert feed_payload["job_delivery_id"] == delivery.job_delivery_id
    assert feed_payload["status"] == "PENDING"
    assert feed_payload["completed_json"] == "[]"
    assert feed_payload["error_code"] is None

    blocked = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert blocked.status_code == 200
    assert blocked.json()["next_step"]["ready"] is False
    assert blocked.json()["next_step"]["readiness_reason"] == "MATERIAL_NOT_READY"
    assert blocked.json()["next_step"]["dispatchable"] is False
    assert blocked.json()["next_step"]["dispatch_block_reason"] == "NOT_READY"
    _assert_rest_unity_readiness_parity(
        client, db_session, job.job_id, expected_ready=False, expected_reason="MATERIAL_NOT_READY"
    )

    deliveries = MaterialDeliveryService(db_session)
    deliveries.start_delivery(delivery.job_delivery_id)
    deliveries.complete_delivery(delivery.job_delivery_id)

    feed_pending = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert feed_pending.status_code == 200
    assert feed_pending.json()["next_step"]["ready"] is False
    assert feed_pending.json()["next_step"]["readiness_reason"] == "MATERIAL_FEED_NOT_READY"
    _assert_rest_unity_readiness_parity(
        client, db_session, job.job_id, expected_ready=False, expected_reason="MATERIAL_FEED_NOT_READY"
    )

    # The feed remains blocked until the normal incoming-QA release workflow
    # records a completed PASS for the latest inspection cycle.
    qa = MaterialInspectionService()
    for item in delivery.items:
        request = qa.request_inspection(db_session, item.delivery_item_id)
        qa.mark_running(db_session, request.inspection_request_id)
        qa.apply_inspection_result(
            db_session,
            IncomingMaterialQAResult(
                inspection_request_id=request.inspection_request_id,
                delivery_item_id=request.delivery_item_id,
                inspection_cycle=request.inspection_cycle,
                result="PASS",
                expected_part_code=request.expected_part_code,
                expected_class_name=request.expected_class_name,
                expected_quantity=request.expected_quantity,
                detected_quantity=request.expected_quantity,
                detections=[],
                frame_width=640,
                frame_height=480,
                camera_source="GLOBAL_CAMERA",
                frame_seq=1,
                timestamp=datetime.now(timezone.utc),
                model_scope="test",
                model_version="1",
                production_valid=True,
            ),
        )
    db_session.commit()

    qa_released = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert qa_released.status_code == 200
    assert qa_released.json()["next_step"]["ready"] is False
    assert qa_released.json()["next_step"]["readiness_reason"] == "MATERIAL_FEED_NOT_READY"
    _assert_rest_unity_readiness_parity(
        client, db_session, job.job_id, expected_ready=False, expected_reason="MATERIAL_FEED_NOT_READY"
    )

    feeds = MaterialFeedExecutionService(db_session)
    feed = feeds.get_for_delivery(delivery.job_delivery_id)
    assert feed is not None
    feeds.start_feed(feed.feed_execution_id)
    feeds.complete_feed(feed.feed_execution_id, completed_slots=())
    ready = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert ready.status_code == 200
    assert ready.json()["next_step"]["ready"] is True
    _assert_rest_unity_readiness_parity(
        client, db_session, job.job_id, expected_ready=True, expected_reason=None
    )


def test_execution_snapshot_covers_pre_roof_and_roof_contract_block(
    client: TestClient,
    db_session: Session,
) -> None:
    product = seed_product_and_steps(db_session)
    job = ProductionOrchestrationService(db_session).create_job(
        product_code=product.product_code,
        job_code="MONITOR-SNAPSHOT-ROOF",
        roof_option_code=RoofOptionCode.ROOF_02,
    )
    seed_complete_incoming_qa(db_session, job_id=job.job_id)
    orchestration = ProductionOrchestrationService(db_session)
    orchestration.start_job(job.job_id)
    for _ in PROCESS_STEP_CODES:
        step = orchestration.get_next_step(job.job_id)
        assert step is not None
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)

    pre_roof = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert pre_roof.status_code == 200
    assert pre_roof.json()["job_status"] == "PRE_ROOF_READY"
    assert pre_roof.json()["current_step"] is None
    assert pre_roof.json()["next_step"] is None
    assert pre_roof.json()["inspection"]["status"] == "PENDING"
    assert pre_roof.json()["roof"]["step"] is None

    lifecycle = ProductionCompletionService(db_session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    roof = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert roof.status_code == 200
    body = roof.json()
    assert body["job_status"] == "ROOF_READY"
    assert body["inspection"]["status"] == "COMPLETED"
    assert body["inspection"]["result"] == "PASS"
    assert body["inspection"]["production_valid"] is True
    assert body["roof"]["roof_option_code"] == "ROOF_02"
    assert body["roof"]["step"]["job_step_id"] == roof_step.job_step_id
    assert body["next_step"]["operation_code"] == "INSTALL_ROOF"
    assert body["next_step"]["ready"] is False
    assert body["next_step"]["readiness_reason"] == "MANUAL_PRESTAGE_REQUIRED"
    assert body["next_step"]["dispatchable"] is False



def test_execution_snapshot_keeps_step_failure_error_code_separate_from_latest_job_event(
    client: TestClient,
    db_session: Session,
) -> None:
    job = create_job(db_session, job_code="MONITOR-SNAPSHOT-E503")
    orchestration = ProductionOrchestrationService(db_session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None
    orchestration.start_step(step.job_step_id)
    orchestration.fail_step(step.job_step_id, reason="synthetic failure", error_code="E503")

    response = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["job_status"] == "FAILED"
    assert body["current_step"] is None
    assert body["next_step"] is None
    assert body["last_event"]["event_type"] == "JOB_FAILED"
    assert body["last_step_execution_event"]["event_type"] == "STEP_FAILED"
    assert body["last_step_execution_event"]["error_code"] == "E503"


def test_execution_snapshot_not_found(client: TestClient) -> None:
    response = client.get("/production/jobs/999999/execution-snapshot")

    assert response.status_code == 404
    assert "Production job not found" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/production/jobs/999", "/production/jobs/999/events"])
def test_missing_production_job_returns_404(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert "Production job not found" in response.json()["detail"]



def _pre_roof_operator_job(session: Session, *, job_code: str):
    product = seed_product_and_steps(session)
    job = ProductionOrchestrationService(session).create_job(
        product_code=product.product_code,
        job_code=job_code,
        roof_option_code=RoofOptionCode.ROOF_01,
    )
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    while (step := orchestration.get_next_step(job.job_id)) is not None:
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)
    session.expire_all()
    return session.get(ProductionJob, job.job_id)


def test_pre_roof_operator_api_has_no_public_trusted_pass_route(
    client: TestClient, db_session: Session,
) -> None:
    job = _pre_roof_operator_job(db_session, job_code="MONITOR-PRE-ROOF-NO-PUBLIC-PASS")
    assert job is not None and job.status is JobStatus.PRE_ROOF_READY

    started = client.post(f"/production/jobs/{job.job_id}/pre-roof/start")
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"
    assert started.json()["result"] is None
    assert started.json()["production_valid"] is False
    assert started.json()["vision_production_valid"] is False
    assert len(started.json()["inspection_request_id"]) == 36

    # PASS is never an HTTP operator authority. It must arrive through the
    # validated Vision Final Result path, leaving this running inspection intact.
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/pass").status_code == 404
    db_session.expire_all()
    inspection = db_session.scalar(select(ProductionInspection).where(
        ProductionInspection.production_job_id == job.job_id
    ))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.RUNNING
    assert db_session.get(ProductionJob, job.job_id).status is JobStatus.PRE_ROOF_READY
    assert db_session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) is None


def test_pre_roof_operator_api_fail_holds_job_and_rejects_direct_pass(
    client: TestClient, db_session: Session,
) -> None:
    job = _pre_roof_operator_job(db_session, job_code="MONITOR-PRE-ROOF-FAIL")
    assert job is not None
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/start").status_code == 200

    failed = client.post(
        f"/production/jobs/{job.job_id}/pre-roof/fail",
        json={"failure_reason": "manual temporary inspection failure"},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "COMPLETED"
    assert failed.json()["result"] == "FAIL"
    assert failed.json()["production_valid"] is False
    assert failed.json()["failure_reason"] == "manual temporary inspection failure"
    stored = db_session.get(ProductionJob, job.job_id)
    assert stored is not None and stored.status is JobStatus.PRE_ROOF_READY
    assert db_session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) is None
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/pass").status_code == 404
    restarted = client.post(f"/production/jobs/{job.job_id}/pre-roof/start")
    assert restarted.status_code == 200
    assert restarted.json()["status"] == "RUNNING"
    assert restarted.json()["inspection_cycle"] == 2
    latest = client.get(f"/production/jobs/{job.job_id}/inspection")
    assert latest.status_code == 200
    assert latest.json()["inspection_cycle"] == 2
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/pass").status_code == 404
    assert db_session.scalar(select(func.count()).select_from(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) == 0


def test_pre_roof_operator_api_rejects_pass_or_fail_before_start(
    client: TestClient, db_session: Session,
) -> None:
    job = _pre_roof_operator_job(db_session, job_code="MONITOR-PRE-ROOF-INVALID")
    assert job is not None
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/pass").status_code == 404
    assert client.post(f"/production/jobs/{job.job_id}/pre-roof/fail", json={}).status_code == 409


def test_selected_job_cancel_uses_orchestration_and_leaves_other_job_untouched(
    client: TestClient,
    db_session: Session,
) -> None:
    job_a = create_job(db_session, job_code="MONITOR-CANCEL-A")
    job_b = ProductionOrchestrationService(db_session).create_job(
        product_code=job_a.product_code,
        job_code="MONITOR-CANCEL-B",
        roof_option_code=RoofOptionCode.ROOF_01,
    )

    response = client.post(f"/production/jobs/{job_a.job_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_a.job_id,
        "job_code": "MONITOR-CANCEL-A",
        "status": "CANCELED",
    }
    assert db_session.get(ProductionJob, job_a.job_id).status is JobStatus.CANCELED
    assert db_session.get(ProductionJob, job_b.job_id).status is JobStatus.REQUESTED
    assert db_session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_id == job_a.job_id,
            ProductionEvent.event_type == EventType.JOB_CANCELED,
        )
    ) is not None

    retry = client.post(f"/production/jobs/{job_a.job_id}/cancel")
    assert retry.status_code == 409
    assert db_session.get(ProductionJob, job_a.job_id).status is JobStatus.CANCELED
