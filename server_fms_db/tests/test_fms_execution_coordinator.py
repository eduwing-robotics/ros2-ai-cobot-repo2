from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import CoordinatorOutcome, FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, RobotCellTaskTypeMapper
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    JobMaterialDelivery,
    JobStatus,
    JobStep,

    Product,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionStatus,
    RoofOptionCode,
    StepStatus,
    EventType,
    ExecutionAttempt,
    ExecutionAttemptStatus,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.step_readiness_service import StepReadinessService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import HOUSE_A_STAGES, HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages

SYNTHETIC_PARTS_JSON = '[{"slot":"SYNTHETIC","class":"base_house_a"}]'


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
                camera_source="D435",
                frame_seq=1,
                timestamp=datetime.now(timezone.utc),
                model_scope="test",
                model_version="1",
                production_valid=True,
            ),
        )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    db.flush()
    add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES))
    add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _job(session: Session, product_code: str = "HOUSE_A", roof: RoofOptionCode | None = RoofOptionCode.ROOF_01):
    product = session.scalar(select(Product).where(Product.product_code == product_code))
    assert product is not None
    service = ProductionOrchestrationService(session)
    job = service.create_job(product_code=product.product_code, job_code=f"COORD-{uuid.uuid4().hex[:12]}", roof_option_code=roof)
    _release_all_materials(session, job, complete_delivery=False)
    return service.start_job(job.job_id)


def _coordinator(session: Session, *results: CellTaskExecutionResult):
    transport = FakeCellActionTransport([FakeCellActionExchange(result) for result in results])
    from shared.services.execution_attempt_service import ExecutionAttemptService
    return FmsExecutionCoordinator(
        session,
        orchestration_service=ProductionOrchestrationService(session),
        step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
        robot_cell_adapter=RobotCellActionAdapter(transport),
        execution_attempt_service=ExecutionAttemptService(session),
    ), transport


def _step(session: Session, job_id: int) -> JobStep:
    step = ProductionOrchestrationService(session).get_next_step(job_id)
    assert step is not None
    return step


def _release_all_materials(session: Session, job, *, complete_delivery: bool = True) -> None:
    deliveries = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)
    for delivery in deliveries:
        # Pre-materialized gated Roof items are QA-visible but have no runtime
        # JobStep yet.  They must remain PENDING until PRE_ROOF binding.
        if all(item.job_step_id is None for item in delivery.items):
            _pass_incoming_qa(session, delivery)
            continue
        _pass_incoming_qa(session, delivery)
        if not complete_delivery:
            continue
        MaterialDeliveryService(session).start_delivery(delivery.job_delivery_id)
        MaterialDeliveryService(session).complete_delivery(delivery.job_delivery_id)
        feed = MaterialFeedExecutionService(session).get_for_delivery(delivery.job_delivery_id)
        # Deferred MANUAL Roof material is intentionally policy-based and has
        # no legacy Feed execution lifecycle.
        if feed is None:
            continue
        feeds = MaterialFeedExecutionService(session)
        feeds.start_feed(feed.feed_execution_id)
        feeds.complete_feed(feed.feed_execution_id, completed_slots=())

def _execute(session: Session, coordinator: FmsExecutionCoordinator, job_id: int):
    step = _step(session, job_id)
    return coordinator.execute_step(
        job_id=job_id,
        job_step_id=step.job_step_id,
        req_id=f"test-job-{job_id}-step-{step.job_step_id}",
        parts_json=SYNTHETIC_PARTS_JSON,
    )


def test_success_uses_goal_acceptance_then_existing_step_lifecycle(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.success())
    step = _step(session, job.job_id)
    result = _execute(session, coordinator, job.job_id)

    events = list(session.scalars(select(ProductionEvent.event_type).where(ProductionEvent.job_id == job.job_id)))
    assert result.outcome is CoordinatorOutcome.COMPLETED and result.started is True
    assert session.get(JobStep, step.job_step_id).status is StepStatus.COMPLETED
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING
    assert events.count(EventType.STEP_STARTED) == events.count(EventType.STEP_COMPLETED) == 1
    assert len(transport.commands) == 1


def test_succeeded_result_with_incomplete_completed_slots_stays_running_for_reconciliation(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.success())
    transport._exchanges = [FakeCellActionExchange(
        CellTaskExecutionResult.success(completed_slots=()),
        complete_requested_slots=False,
    )]
    step = _step(session, job.job_id)

    result = _execute(session, coordinator, job.job_id)
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.req_id == result.req_id))

    assert result.outcome is CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR
    assert result.started and result.requires_reconciliation
    assert session.get(JobStep, step.job_step_id).status is StepStatus.RUNNING
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.UNKNOWN
    assert len(transport.commands) == 1


def test_empty_parts_json_is_blocked_before_execution_attempt_or_transport_send(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.success())
    step = _step(session, job.job_id)

    result = coordinator.execute_step(
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        req_id="empty-parts",
        parts_json="[]",
    )

    assert result.outcome is CoordinatorOutcome.CONTRACT_BLOCKED
    assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
    assert transport.commands == []


@pytest.mark.parametrize(
    ("cell_result", "expected"),
    [
        (CellTaskExecutionResult.goal_rejected(detail="no"), CoordinatorOutcome.GOAL_REJECTED),
        (CellTaskExecutionResult.server_unavailable(detail="offline"), CoordinatorOutcome.SERVER_UNAVAILABLE),
        (CellTaskExecutionResult.transport_error(detail="connection failed"), CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR),
    ],
)
def test_unaccepted_dispatch_never_starts_step(session: Session, cell_result: CellTaskExecutionResult, expected: CoordinatorOutcome) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, cell_result)
    step = _step(session, job.job_id)
    result = _execute(session, coordinator, job.job_id)

    assert result.outcome is expected and result.started is False
    assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING
    assert session.scalar(
        select(func.count()).select_from(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    ) == 0
    assert len(transport.commands) == 1


def test_accepted_timeout_keeps_running_and_prevents_redispatch(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.result_timeout(detail="waiting"))
    step = _step(session, job.job_id)
    timeout = _execute(session, coordinator, job.job_id)
    repeat = coordinator.execute_step(job_id=job.job_id, job_step_id=step.job_step_id, req_id="repeat", parts_json=SYNTHETIC_PARTS_JSON)

    assert timeout.outcome is CoordinatorOutcome.UNCERTAIN_RESULT_TIMEOUT
    assert timeout.started and timeout.requires_reconciliation
    assert session.get(JobStep, step.job_step_id).status is StepStatus.RUNNING
    assert repeat.outcome is CoordinatorOutcome.ALREADY_RUNNING
    assert session.scalar(
        select(func.count()).select_from(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    ) == 0
    assert len(transport.commands) == 1


def test_authoritative_cell_failure_uses_fail_step_and_keeps_error_code_in_result(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.cell_failed(error_code="E503", detail="synthetic failure"))
    step = _step(session, job.job_id)
    result = _execute(session, coordinator, job.job_id)

    stored = session.get(JobStep, step.job_step_id)
    assert result.outcome is CoordinatorOutcome.CELL_FAILED
    assert result.cell_result is not None and result.cell_result.error_code == "E503"
    assert stored is not None and stored.status is StepStatus.FAILED
    assert stored.failure_reason == "synthetic failure"
    assert session.get(type(job), job.job_id).status is JobStatus.FAILED
    step_failed = session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    )
    job_failed = session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_id == job.job_id,
            ProductionEvent.event_type == EventType.JOB_FAILED,
        )
    )
    assert step_failed is not None and step_failed.error_code == "E503"
    assert job_failed is not None and job_failed.error_code is None
    assert len(transport.commands) == 1


def test_authoritative_cell_canceled_is_preserved_without_failed_lifecycle_mutation(session: Session) -> None:
    job = _job(session)
    coordinator, transport = _coordinator(
        session, CellTaskExecutionResult.cell_canceled(detail="operator canceled", completed_slots=("SYNTHETIC",))
    )
    step = _step(session, job.job_id)

    result = _execute(session, coordinator, job.job_id)

    assert result.outcome is CoordinatorOutcome.CELL_CANCELED
    assert result.cell_result is not None and result.cell_result.completed_slots == ("SYNTHETIC",)
    assert result.started is True and result.requires_reconciliation is True
    assert session.get(JobStep, step.job_step_id).status is StepStatus.RUNNING
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING
    assert session.scalar(
        select(func.count()).select_from(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    ) == 0
    assert len(transport.commands) == 1


def test_cell_failure_without_error_code_keeps_event_error_code_null(session: Session) -> None:
    job = _job(session)
    coordinator, _ = _coordinator(session, CellTaskExecutionResult.cell_failed(error_code=None, detail="uncoded failure"))
    step = _step(session, job.job_id)

    result = _execute(session, coordinator, job.job_id)

    event = session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    )
    assert result.outcome is CoordinatorOutcome.CELL_FAILED
    assert event is not None and event.error_code is None
    assert session.get(JobStep, step.job_step_id).failure_reason == "uncoded failure"


def test_material_delivery_readiness_blocks_then_allows_dispatch(session: Session) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None
    from shared.models.factory import Part, PartCategory
    part = Part(vision_class="wall_ext_back", part_code="SYNTH_PART", part_name="Synthetic", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part); session.flush()
    from shared.models.factory import Inventory
    session.add(Inventory(part_code=part.part_code, quantity=100))
    recipe.stages[0].part_code = part.part_code
    recipe.stages[0].quantity = 1
    session.commit()

    job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.success())
    first = _step(session, job.job_id)
    blocked = _execute(session, coordinator, job.job_id)
    delivery = session.scalar(select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id))
    assert delivery is not None
    deliveries = MaterialDeliveryService(session)
    deliveries.start_delivery(delivery.job_delivery_id); deliveries.complete_delivery(delivery.job_delivery_id)
    _pass_incoming_qa(session, delivery)
    feed = MaterialFeedExecutionService(session).get_for_delivery(delivery.job_delivery_id)
    assert feed is not None
    feeds = MaterialFeedExecutionService(session)
    feeds.start_feed(feed.feed_execution_id)
    feeds.complete_feed(feed.feed_execution_id, completed_slots=())
    completed = _execute(session, coordinator, job.job_id)

    assert blocked.outcome is CoordinatorOutcome.NOT_DISPATCHED_NOT_READY
    assert completed.outcome is CoordinatorOutcome.COMPLETED
    assert session.get(JobStep, first.job_step_id).status is StepStatus.COMPLETED
    assert len(transport.commands) == 1


@pytest.mark.parametrize("product_code", ["HOUSE_A", "HOUSE_B"])
def test_fake_cell_runs_actual_recipe_to_pre_roof_ready(session: Session, product_code: str) -> None:
    job = _job(session, product_code)
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == product_code))
    assert recipe is not None
    steps = list(session.scalars(select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)))
    _release_all_materials(session, job)
    coordinator, transport = _coordinator(session, *[CellTaskExecutionResult.success() for _ in steps])
    results = [_execute(session, coordinator, job.job_id) for _ in steps]

    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert all(result.outcome is CoordinatorOutcome.COMPLETED for result in results)
    assert [item.task_type for item in transport.commands] == [RobotCellTaskTypeMapper.map_operation_code(step.operation_code) for step in steps]
    assert [item.product for item in transport.commands] == [product_code] * len(steps)
    assert [item.req_id for item in transport.commands] == [f"test-job-{job.job_id}-step-{step.job_step_id}" for step in steps]
    assert all(step.status is StepStatus.COMPLETED for step in steps)
    assert session.get(type(job), job.job_id).status is JobStatus.PRE_ROOF_READY
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF")) == 0


def test_pre_roof_pass_keeps_roof_dispatch_contract_blocked(session: Session) -> None:
    job = _job(session, roof=RoofOptionCode.ROOF_02)
    stage_count = session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id == job.job_id, JobStep.source_recipe_stage_id.is_not(None)))
    assert stage_count is not None
    _release_all_materials(session, job)
    coordinator, transport = _coordinator(session, *[CellTaskExecutionResult.success() for _ in range(stage_count)])
    for _ in range(stage_count):
        assert _execute(session, coordinator, job.job_id).outcome is CoordinatorOutcome.COMPLETED
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    blocked = coordinator.execute_step(job_id=job.job_id, job_step_id=roof_step.job_step_id, req_id="roof-blocked", parts_json=SYNTHETIC_PARTS_JSON)

    assert blocked.outcome is CoordinatorOutcome.NOT_DISPATCHED_NOT_READY
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.PENDING
    assert session.get(type(job), job.job_id).status is JobStatus.ROOF_READY
    assert len(transport.commands) == stage_count


def test_pre_roof_pass_allows_roof_goal_when_caller_supplies_verified_roof_parts(session: Session) -> None:
    job = _job(session, roof=RoofOptionCode.ROOF_02)
    stage_count = session.scalar(
        select(func.count()).select_from(JobStep).where(
            JobStep.job_id == job.job_id, JobStep.source_recipe_stage_id.is_not(None)
        )
    )
    assert stage_count is not None
    _release_all_materials(session, job)
    coordinator, transport = _coordinator(
        session, *[CellTaskExecutionResult.success() for _ in range(stage_count + 1)]
    )
    for _ in range(stage_count):
        assert _execute(session, coordinator, job.job_id).outcome is CoordinatorOutcome.COMPLETED
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)

    roof_delivery = MaterialDeliveryService(session).get_required_deliveries_for_step(roof_step.job_step_id)[0]
    _pass_incoming_qa(session, roof_delivery)
    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job.job_id, job_delivery_id=roof_delivery.job_delivery_id, request_id="roof-prestage"
    )

    result = coordinator.execute_step(
        job_id=job.job_id,
        job_step_id=roof_step.job_step_id,
        req_id="roof-verified-payload",
        parts_json='[{"slot":"SYNTHETIC_ROOF_SLOT","class":"roof_02","part_code":"ROOF_02"}]',
    )

    assert result.outcome is CoordinatorOutcome.COMPLETED
    assert transport.commands[-1].task_type == "INSTALL_ROOF"
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.COMPLETED
    assert session.get(type(job), job.job_id).status is JobStatus.ROOF_READY
    ProductionOrchestrationService(session).complete_job(job.job_id)
    assert session.get(type(job), job.job_id).status is JobStatus.COMPLETED


def test_completed_or_failed_step_is_never_redispatched(session: Session) -> None:
    completed_job = _job(session)
    coordinator, transport = _coordinator(session, CellTaskExecutionResult.success())
    step = _step(session, completed_job.job_id)
    assert _execute(session, coordinator, completed_job.job_id).outcome is CoordinatorOutcome.COMPLETED
    completed = coordinator.execute_step(job_id=completed_job.job_id, job_step_id=step.job_step_id, req_id="completed", parts_json=SYNTHETIC_PARTS_JSON)

    failed_job = _job(session)
    failing, failing_transport = _coordinator(session, CellTaskExecutionResult.cell_failed(error_code="E1", detail="fail"))
    failed_step = _step(session, failed_job.job_id)
    assert _execute(session, failing, failed_job.job_id).outcome is CoordinatorOutcome.CELL_FAILED
    failed = failing.execute_step(job_id=failed_job.job_id, job_step_id=failed_step.job_step_id, req_id="failed", parts_json=SYNTHETIC_PARTS_JSON)

    assert completed.outcome is CoordinatorOutcome.ALREADY_TERMINAL
    assert failed.outcome is CoordinatorOutcome.ALREADY_TERMINAL
    assert len(transport.commands) == len(failing_transport.commands) == 1
