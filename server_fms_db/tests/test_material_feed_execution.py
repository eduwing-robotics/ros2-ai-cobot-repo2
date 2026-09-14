from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import CoordinatorOutcome, FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.material_feed_execution_coordinator import (
    MaterialFeedCoordinatorOutcome,
    MaterialFeedExecutionCoordinator,
)
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe, JobMaterialDelivery, JobMaterialFeedExecution, JobStatus,
    JobStep, MaterialDeliveryStatus, MaterialFeedStatus, Part,
    PartCategory, Product, StepStatus,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService
from tests.recipe_test_support import HOUSE_A_STAGES, add_active_recipe, seed_inventory_for_recipe

SYNTHETIC_FEED_PARTS = '[{"slot":"SYNTH_FEED_SLOT_1","class":"furniture_bath","part_code":"SYNTH_FEED_PART_1"},{"slot":"SYNTH_FEED_SLOT_2","class":"furniture_bath"}]'
SYNTHETIC_INSTALL_PARTS = '[{"slot":"SYNTH_INSTALL_SLOT","class":"base_house_a"}]'


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all([
        Product(product_code="HOUSE_A", product_name="A형 주택"),
        Part(vision_class="wall_ext_back", part_code="SYNTH_PART", part_name="Synthetic", category=PartCategory.STRUCTURE, unit="EA"),
    ])
    db.flush()
    add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES, materialized=False)
    recipe = db.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    part = db.scalar(select(Part).where(Part.part_code == "SYNTH_PART"))
    assert recipe is not None and part is not None
    product = db.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _complete_delivery(session: Session, delivery: JobMaterialDelivery) -> None:
    service = MaterialDeliveryService(session)
    service.start_delivery(delivery.job_delivery_id)
    service.complete_delivery(delivery.job_delivery_id)


def _pass_incoming_qa(session: Session, delivery: JobMaterialDelivery) -> None:
    service = MaterialInspectionService()
    for item in delivery.items:
        request = service.request_inspection(session, item.delivery_item_id)
        service.mark_running(session, request.inspection_request_id)
        service.apply_inspection_result(
            session,
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


def _feed_coordinator(session: Session, *results: CellTaskExecutionResult) -> tuple[MaterialFeedExecutionCoordinator, FakeCellActionTransport]:
    transport = FakeCellActionTransport([FakeCellActionExchange(result) for result in results])
    coordinator = MaterialFeedExecutionCoordinator(session, robot_cell_adapter=RobotCellActionAdapter(transport))
    return coordinator, transport


def _job(session: Session):
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None

    # Needs parts for the steps to be eligible for material delivery
    part = session.scalar(select(Part).where(Part.part_code == "SYNTH_PART"))
    for stage in recipe.stages[:1]:
        stage.part_code = part.part_code
        stage.quantity = 1
    seed_inventory_for_recipe(session, recipe)
    session.flush()

    job = ProductionOrchestrationService(session).create_job(product_code=product.product_code, job_code="FEED-JOB")
    ProductionOrchestrationService(session).start_job(job.job_id)
    step = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id))
    deliveries = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)
    feeds = session.scalars(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == deliveries[0].job_delivery_id)).all()
    return job, step, deliveries, feeds


def test_readiness_distinguishes_delivery_then_feed_prerequisites(session: Session) -> None:
    job, step, deliveries, feeds = _job(session)
    readiness = StepReadinessService(MaterialDeliveryService(session))
    assert readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
    _pass_incoming_qa(session, deliveries[0])
    assert readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id).reason is StepReadinessReason.MATERIAL_NOT_READY
    _complete_delivery(session, deliveries[0])
    assert readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id).reason is StepReadinessReason.MATERIAL_FEED_NOT_READY
    service = MaterialFeedExecutionService(session)
    for feed in feeds:
        service.start_feed(feed.feed_execution_id)
        service.complete_feed(feed.feed_execution_id, completed_slots=())
    assert readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id).ready is True


def test_pending_delivery_blocks_feed_dispatch_without_transport_send(session: Session) -> None:
    _, _, _, feeds = _job(session)
    coordinator, transport = _feed_coordinator(session, CellTaskExecutionResult.success())

    result = coordinator.execute_feed(
        feed_execution_id=feeds[0].feed_execution_id,
        req_id="feed-before-delivery",
        parts_json=SYNTHETIC_FEED_PARTS,
    )

    assert result.outcome is MaterialFeedCoordinatorOutcome.NOT_ELIGIBLE
    assert session.get(JobMaterialFeedExecution, feeds[0].feed_execution_id).status is MaterialFeedStatus.PENDING
    assert transport.commands == []


def test_feed_success_unblocks_install_without_mutating_step_or_job(session: Session) -> None:
    job, step, deliveries, feeds = _job(session)
    _complete_delivery(session, deliveries[0])
    _pass_incoming_qa(session, deliveries[0])
    coordinator, transport = _feed_coordinator(session, CellTaskExecutionResult.success(completed_slots=("SYNTH_FEED_SLOT_1",)))
    result = coordinator.execute_feed(feed_execution_id=feeds[0].feed_execution_id, req_id="feed-a", parts_json=SYNTHETIC_FEED_PARTS)
    assert result.outcome is MaterialFeedCoordinatorOutcome.COMPLETED
    assert transport.commands[0].task_type == "MATERIAL_FEED"
    assert transport.commands[0].step_id == f"MATERIAL-FEED-{feeds[0].feed_execution_id}"
    stored = session.get(JobMaterialFeedExecution, feeds[0].feed_execution_id)
    assert stored is not None and stored.status is MaterialFeedStatus.COMPLETED
    assert json.loads(stored.completed_json) == ["SYNTH_FEED_SLOT_1"]
    assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING

    install_transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    from shared.services.execution_attempt_service import ExecutionAttemptService
    install = FmsExecutionCoordinator(
        session,
        orchestration_service=ProductionOrchestrationService(session),
        step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
        robot_cell_adapter=RobotCellActionAdapter(install_transport),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    installed = install.execute_step(job_id=job.job_id, job_step_id=step.job_step_id, req_id="install-a", parts_json=SYNTHETIC_INSTALL_PARTS)
    assert installed.outcome is CoordinatorOutcome.COMPLETED


@pytest.mark.parametrize(
    ("cell_result", "expected", "status"),
    [
        (CellTaskExecutionResult.goal_rejected(detail="rejected"), MaterialFeedCoordinatorOutcome.GOAL_REJECTED, MaterialFeedStatus.PENDING),
        (CellTaskExecutionResult.server_unavailable(detail="offline"), MaterialFeedCoordinatorOutcome.SERVER_UNAVAILABLE, MaterialFeedStatus.PENDING),
        (CellTaskExecutionResult.result_timeout(detail="wait"), MaterialFeedCoordinatorOutcome.UNCERTAIN_RESULT_TIMEOUT, MaterialFeedStatus.RUNNING),
        (CellTaskExecutionResult.cell_canceled(detail="canceled"), MaterialFeedCoordinatorOutcome.CELL_CANCELED, MaterialFeedStatus.RUNNING),
    ],
)
def test_feed_non_success_outcomes_do_not_unblock_install(session: Session, cell_result, expected, status) -> None:
    _, _, deliveries, feeds = _job(session)
    _complete_delivery(session, deliveries[0])
    coordinator, transport = _feed_coordinator(session, cell_result)
    result = coordinator.execute_feed(feed_execution_id=feeds[0].feed_execution_id, req_id="feed-non-success", parts_json=SYNTHETIC_FEED_PARTS)
    assert result.outcome is expected
    assert session.get(JobMaterialFeedExecution, feeds[0].feed_execution_id).status is status
    assert len(transport.commands) == 1


def test_failed_feed_preserves_error_and_completed_json_without_failing_job(session: Session) -> None:
    job, step, deliveries, feeds = _job(session)
    _complete_delivery(session, deliveries[0])
    coordinator, _ = _feed_coordinator(session, CellTaskExecutionResult.cell_failed(error_code="E203", detail="synthetic", completed_slots=("SYNTH_FEED_SLOT_1",)))
    result = coordinator.execute_feed(feed_execution_id=feeds[0].feed_execution_id, req_id="feed-fail", parts_json=SYNTHETIC_FEED_PARTS)
    stored = session.get(JobMaterialFeedExecution, feeds[0].feed_execution_id)
    assert result.outcome is MaterialFeedCoordinatorOutcome.CELL_FAILED
    assert stored.status is MaterialFeedStatus.FAILED and stored.error_code == "E203"
    assert stored.failure_reason == "synthetic" and json.loads(stored.completed_json) == ["SYNTH_FEED_SLOT_1"]
    assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING


def test_feed_stable_step_id_is_reused_with_new_req_id(session: Session) -> None:
    _, _, deliveries, feeds = _job(session)
    _complete_delivery(session, deliveries[0])
    coordinator, transport = _feed_coordinator(session, CellTaskExecutionResult.goal_rejected(), CellTaskExecutionResult.goal_rejected())
    first = coordinator.execute_feed(feed_execution_id=feeds[0].feed_execution_id, req_id="feed-a", parts_json=SYNTHETIC_FEED_PARTS)
    second = coordinator.execute_feed(feed_execution_id=feeds[0].feed_execution_id, req_id="feed-b", parts_json=SYNTHETIC_FEED_PARTS)
    assert first.stable_step_id == second.stable_step_id == f"MATERIAL-FEED-{feeds[0].feed_execution_id}"
    assert [command.req_id for command in transport.commands] == ["feed-a", "feed-b"]
