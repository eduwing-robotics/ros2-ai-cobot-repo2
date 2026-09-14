"""Deterministic fake closure for the HOUSE_B software Happy Path."""

from __future__ import annotations

from collections.abc import Iterator
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from api_server.main import app
from api_server.routers.inventory import get_db
from api_server.routers.production import get_operator_empty_pallet_return_service
from api_server.services.production_snapshot_service import ProductionSnapshotService

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.empty_pallet_return_service import EmptyPalletReturnNotEligibleError
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.operator_empty_pallet_return_service import OperatorEmptyPalletReturnService
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE, seed_house_b_mvp_master
from tests.recipe_test_support import seed_inventory_for_recipe
from shared.config import MaterialPrefetchMode
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
    ProductionInspection,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    ProductionInspectionType,
    RoofOptionCode,
    StepStatus,
)
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.operator_execution_ready_service import OperatorExecutionReadyService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.physical_ready_service import PhysicalReadyService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.production_status_query_service import ProductionStatusQueryService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.drop_resource_service import DropResourceService, DropResourceState
from shared.services.unity_current_stage_projection_service import UnityCurrentStageProjectionService


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="session")
def session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as db:
        yield db


def _worker(
    session_factory: sessionmaker[Session],
    *,
    forklift_result: ForkliftExecutionResult | None = None,
    auto_empty_pallet_return_enabled: bool = True,
    material_prefetch_mode: MaterialPrefetchMode | str = MaterialPrefetchMode.DISABLED,
) -> tuple[FmsWorker, FakeForkliftActionTransport, FakeCellActionTransport]:
    forklift = FakeForkliftActionTransport(execute_transport_result=forklift_result)
    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(8)
    ])
    cell_adapter = RobotCellActionAdapter(cell)
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
            db, adapter=forklift_adapter, execution_attempt_service=ExecutionAttemptService(db)
        )

    def feed_factory(db: Session) -> MaterialFeedExecutionCoordinator:
        return MaterialFeedExecutionCoordinator(db, robot_cell_adapter=cell_adapter)

    return FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_factory,
        forklift_execution_coordinator_factory=forklift_factory,
        material_feed_execution_coordinator_factory=feed_factory,
        auto_empty_pallet_return_enabled=auto_empty_pallet_return_enabled,
        material_prefetch_mode=material_prefetch_mode,
    ), forklift, cell


def _pass_all_incoming_qa(session: Session, job_id: int) -> None:
    service = MaterialInspectionService()
    items = list(session.scalars(
        select(JobMaterialDeliveryItem)
        .join(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job_id)
        .order_by(JobMaterialDeliveryItem.delivery_item_id)
    ))
    assert len(items) == 7
    for item in items:
        request = service.request_inspection(session, item.delivery_item_id)
        service.mark_running(session, request.inspection_request_id)
        service.apply_inspection_result(
            session,
            IncomingMaterialQAResult(
                inspection_request_id=request.inspection_request_id,
                delivery_item_id=item.delivery_item_id,
                inspection_cycle=request.inspection_cycle,
                status="COMPLETED",
                result="PASS",
                failure_type=None,
                expected_part_code=request.expected_part_code,
                expected_class_name=request.expected_class_name,
                expected_quantity=item.quantity,
                detected_quantity=item.quantity,
                detections=[],
                frame_width=640,
                frame_height=480,
                camera_source="GLOBAL_CAMERA",
                frame_seq=request.inspection_cycle,
                timestamp=datetime.now(timezone.utc),
                model_scope="fake-house-b",
                model_version="1",
                production_valid=True,
            ),
        )
        session.commit()


def _job_with_house_b_master(session: Session, code: str):
    recipe = seed_house_b_mvp_master(session)
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return ProductionOrchestrationService(session).create_job(
        product_code=HOUSE_B_PRODUCT_CODE,
        job_code=code,
        roof_option_code=RoofOptionCode.ROOF_02,
    )


def _delivery_by_group(session: Session, job_id: int) -> dict[str, JobMaterialDelivery]:
    return {
        delivery.supply_group_code: delivery
        for delivery in session.scalars(select(JobMaterialDelivery).where(
            JobMaterialDelivery.production_job_id == job_id
        ))
    }


def _mark_material_arrived(
    session: Session,
    *,
    job_id: int,
    delivery: JobMaterialDelivery,
    pickup_code: str,
) -> None:
    """Persist the existing material-arrival evidence without dispatching hardware."""
    delivery.status = MaterialDeliveryStatus.COMPLETED
    session.add(
        ExecutionAttempt(
            req_id=f"material-arrived-{delivery.job_delivery_id}",
            executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT",
            job_id=job_id,
            job_delivery_id=delivery.job_delivery_id,
            attempt_no=1,
            status=ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json=json.dumps(
                {"pickup_code": pickup_code, "dropoff_code": "DROP"},
                sort_keys=True,
            ),
        )
    )
    session.commit()


def _operator_return(session: Session, forklift: FakeForkliftActionTransport, job_id: int, delivery_id: int) -> None:
    coordinator = ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(forklift),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    result = OperatorEmptyPalletReturnService(
        session, forklift_execution_coordinator=coordinator
    ).execute_confirmed_empty_return(
        production_job_id=job_id, job_delivery_id=delivery_id
    )
    assert result.status.value == "SUCCEEDED"


def test_pre_roof_ready_stays_worker_held_but_roof_ready_dispatches_once(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-ROOF-WORKER")
    _pass_all_incoming_qa(session, job.job_id)
    deliveries = _delivery_by_group(session, job.job_id)
    # Complete the six initial steps through the authoritative lifecycle; the
    # test is about Worker behavior only once the runtime Roof step exists.
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    for _ in range(6):
        step = orchestration.get_next_step(job.job_id)
        assert step is not None
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)
    # The new lifecycle holds PRE_ROOF until durable inner pallet return.
    session.add(ExecutionAttempt(
        req_id="inner-return-roof-worker", executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT_EMPTY_RETURN", job_id=job.job_id,
        job_delivery_id=deliveries["INNER_WALL"].job_delivery_id, attempt_no=1,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
    ))
    session.commit()
    orchestration.try_enter_pre_roof_ready_after_inner_return(job.job_id)
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    worker, _, cell = _worker(session_factory)
    assert worker.tick() is False  # PRE_ROOF_READY has no runtime step yet.
    assert cell.commands == []

    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job.job_id,
        job_delivery_id=deliveries["ROOF"].job_delivery_id,
        request_id="roof-worker-prestage",
    )
    assert worker.tick() is True
    session.refresh(roof_step)
    session.refresh(job)
    assert roof_step.status is StepStatus.COMPLETED
    assert job.status is JobStatus.ROOF_READY
    # A new/restarted FMS tick has no pending frontier after the completed
    # roof.  It must neither re-dispatch it nor create a second Cell attempt.
    attempt_count = session.scalar(select(func.count()).select_from(ExecutionAttempt).where(
        ExecutionAttempt.job_step_id == roof_step.job_step_id,
        ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
    ))
    assert [command.task_type for command in cell.commands] == ["INSTALL_ROOF"]
    assert worker.tick() is False
    assert len(cell.commands) == 1
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt).where(
        ExecutionAttempt.job_step_id == roof_step.job_step_id,
        ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
    )) == attempt_count
    ProductionOrchestrationService(session).complete_job(job.job_id)
    assert job.status is JobStatus.COMPLETED


def test_operator_empty_return_requires_all_consuming_steps_and_rejects_manual(
    session: Session,
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-RETURN-GUARDS")
    _pass_all_incoming_qa(session, job.job_id)
    deliveries = _delivery_by_group(session, job.job_id)
    outer = deliveries["OUTER_WALLS"]
    inner = deliveries["INNER_WALL"]
    base = deliveries["BASE"]
    # Model only already-arrived pallets; successful material Attempt is the
    # durable DROP ownership fact needed by Phase C.
    for delivery in (inner, outer):
        delivery.status = MaterialDeliveryStatus.COMPLETED
        session.add(ExecutionAttempt(
            req_id=f"material-{delivery.job_delivery_id}", executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT", job_id=job.job_id,
            job_delivery_id=delivery.job_delivery_id, attempt_no=1,
            status=__import__("shared.models.factory", fromlist=["ExecutionAttemptStatus"]).ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json='{"pickup_code":"RACK1","dropoff_code":"DROP"}',
        ))
    session.commit()
    fake = FakeForkliftActionTransport()
    coordinator = ForkliftExecutionCoordinator(session, adapter=ForkliftActionAdapter(fake), execution_attempt_service=ExecutionAttemptService(session))
    service = OperatorEmptyPalletReturnService(session, forklift_execution_coordinator=coordinator)

    outer_item_step_ids = list(session.scalars(select(JobMaterialDeliveryItem.job_step_id).where(
        JobMaterialDeliveryItem.job_delivery_id == outer.job_delivery_id
    )))
    assert len(outer_item_step_ids) == 4 and all(step_id is not None for step_id in outer_item_step_ids)
    for step_id in outer_item_step_ids[:3]:
        session.get(JobStep, step_id).status = StepStatus.COMPLETED
    session.commit()
    with pytest.raises(EmptyPalletReturnNotEligibleError, match="consuming JobSteps"):
        service.execute_confirmed_empty_return(production_job_id=job.job_id, job_delivery_id=outer.job_delivery_id)
    assert fake.execute_transport_requests == []

    inner_step_id = session.scalar(select(JobMaterialDeliveryItem.job_step_id).where(JobMaterialDeliveryItem.job_delivery_id == inner.job_delivery_id))
    assert inner_step_id is not None
    session.get(JobStep, inner_step_id).status = StepStatus.COMPLETED
    session.commit()
    # INNER cannot return while OUTER's artificial material attempt still owns
    # DROP. Remove it to isolate consumption validation; Phase C covers owner races.
    session.query(ExecutionAttempt).filter(ExecutionAttempt.job_delivery_id == outer.job_delivery_id).delete()
    session.commit()
    assert service.execute_confirmed_empty_return(production_job_id=job.job_id, job_delivery_id=inner.job_delivery_id).status.value == "SUCCEEDED"

    base_step_id = session.scalar(select(JobMaterialDeliveryItem.job_step_id).where(
        JobMaterialDeliveryItem.job_delivery_id == base.job_delivery_id
    ))
    assert base_step_id is not None
    session.get(JobStep, base_step_id).status = StepStatus.COMPLETED
    session.commit()
    with pytest.raises(EmptyPalletReturnNotEligibleError, match="TRANSPORTED"):
        service.execute_confirmed_empty_return(production_job_id=job.job_id, job_delivery_id=base.job_delivery_id)


@pytest.mark.parametrize(
    "material_prefetch_mode",
    [MaterialPrefetchMode.DISABLED, MaterialPrefetchMode.ONE_AHEAD],
    ids=["disabled-legacy-serial", "one-ahead-prefetch"],
)
def test_house_b_full_fake_happy_path_to_completed(
    session: Session,
    session_factory: sessionmaker[Session],
    material_prefetch_mode: MaterialPrefetchMode,
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-HAPPY-CLOSURE")
    stage_projection = UnityCurrentStageProjectionService(session)
    assert stage_projection.derive(job=job) == "JOB_REQUESTED"
    initial_steps = list(session.scalars(select(JobStep).where(JobStep.job_id == job.job_id)))
    roof_item = session.scalar(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.part_code == "ROOF-ZIP-01",
        JobMaterialDeliveryItem.job_delivery_id.in_(select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id == job.job_id)),
    ))
    assert len(initial_steps) == 6 and roof_item is not None and roof_item.job_step_id is None
    roof_item_id = roof_item.delivery_item_id

    _pass_all_incoming_qa(session, job.job_id)
    assert stage_projection.derive(job=job) == "INSTALL_BASE"
    deliveries = _delivery_by_group(session, job.job_id)
    # Full global QA is open.  The recipe snapshot now makes OUTER the first
    # transported group after Base; INNER remains unavailable until all outer
    # work and its pallet return have completed.
    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job.job_id, job_delivery_id=deliveries["BASE"].job_delivery_id, request_id="base-ready"
    )
    PhysicalReadyService(session).confirm_physical_ready(
        job_delivery_id=deliveries["OUTER_WALLS"].job_delivery_id, request_id="outer-ready"
    )
    worker, forklift, cell = _worker(
        session_factory, material_prefetch_mode=material_prefetch_mode
    )

    if material_prefetch_mode is MaterialPrefetchMode.DISABLED:
        assert worker.tick() is True  # Base fake success
        session.expire_all()
        base_step = session.scalar(select(JobStep).where(
            JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_BASE",
        ))
        assert base_step is not None and base_step.status is StepStatus.COMPLETED
        assert stage_projection.derive(job=job) == "DELIVER_OUTER_WALL"
        assert worker.tick() is True  # OUTER RACK1 -> DROP
    else:
        # One-ahead prefetches the next recipe-ordered transported group, OUTER,
        # before Base starts.
        assert worker.tick() is True
        session.expire_all()
        base_step = session.scalar(select(JobStep).where(
            JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_BASE",
        ))
        assert base_step is not None and base_step.status is StepStatus.PENDING
        assert deliveries["OUTER_WALLS"].status is MaterialDeliveryStatus.COMPLETED
        assert cell.commands == []
        assert stage_projection.derive(job=job) == "INSTALL_BASE"
        assert worker.tick() is True  # Base fake success

    session.expire_all()
    assert deliveries["OUTER_WALLS"].status is MaterialDeliveryStatus.COMPLETED
    assert stage_projection.derive(job=job) == "INSTALL_OUTER_WALL"
    outer_steps = list(session.scalars(
        select(JobStep).where(
            JobStep.job_id == job.job_id,
            JobStep.supply_group_code == "OUTER_WALLS",
        ).order_by(JobStep.step_order)
    ))
    assert len(outer_steps) == 4
    for index, outer_step in enumerate(outer_steps):
        OperatorExecutionReadyService(session).confirm(
            job_id=job.job_id, job_step_id=outer_step.job_step_id,
        )
        assert worker.tick() is True
        if index < 3:
            assert stage_projection.derive(job=job) == "INSTALL_OUTER_WALL"
    session.expire_all()
    assert all(step.status is StepStatus.COMPLETED for step in outer_steps)
    assert stage_projection.derive(job=job) == "DELIVER_INNER_WALL"
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE

    PhysicalReadyService(session).confirm_physical_ready(
        job_delivery_id=deliveries["INNER_WALL"].job_delivery_id, request_id="inner-ready"
    )
    assert stage_projection.derive(job=job) == "DELIVER_INNER_WALL"
    assert worker.tick() is True  # INNER RACK2 -> DROP
    session.expire_all()
    assert deliveries["INNER_WALL"].status is MaterialDeliveryStatus.COMPLETED
    assert stage_projection.derive(job=job) == "INSTALL_INNER_WALL"
    inner_step = session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_INNER_WALL",
    ))
    assert inner_step is not None
    OperatorExecutionReadyService(session).confirm(
        job_id=job.job_id, job_step_id=inner_step.job_step_id,
    )
    assert worker.tick() is True
    session.expire_all()
    assert inner_step.status is StepStatus.COMPLETED

    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    # PRE_ROOF remains the representative production stage while OUTER return
    # and ReturnHome are visible through transport_status.
    assert stage_projection.derive(job=job) == "PRE_ROOF_INSPECTION"
    lifecycle = ProductionCompletionService(session)
    first_cycle = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    lifecycle.fail_pre_roof_inspection(
        production_job_id=job.job_id,
        reason="fake pre-roof rework required",
    )
    session.refresh(job)
    assert job.status is JobStatus.PRE_ROOF_READY
    assert stage_projection.derive(job=job) == "PRE_ROOF_INSPECTION"
    second_cycle = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert (first_cycle.inspection_cycle, second_cycle.inspection_cycle) == (1, 2)
    # The latest RUNNING cycle, not cycle 1's historical FAIL, remains the
    # authoritative PRE_ROOF display state.
    assert stage_projection.derive(job=job) == "PRE_ROOF_INSPECTION"
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    inspection_history = list(session.scalars(
        select(ProductionInspection)
        .where(ProductionInspection.production_job_id == job.job_id)
        .order_by(ProductionInspection.inspection_cycle)
    ))
    assert [(inspection.status, inspection.result) for inspection in inspection_history] == [
        (ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.FAIL),
        (ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.PASS),
    ]
    assert stage_projection.derive(job=job) == "INSTALL_ROOF"
    session.refresh(roof_item)
    assert roof_item.delivery_item_id == roof_item_id and roof_item.job_step_id == roof_step.job_step_id
    ManualPrestageService(session).confirm_manual_prestage_ready(job_id=job.job_id, job_delivery_id=deliveries["ROOF"].job_delivery_id, request_id="roof-ready")
    assert worker.tick() is True
    # Explicit outbound checkpoint completion, after the roof Action succeeds.
    ProductionOrchestrationService(session).complete_job(job.job_id)

    session.expire_all()
    session.refresh(job)
    assert job.status is JobStatus.COMPLETED
    assert stage_projection.derive(job=job) == "INSTALL_ROOF"
    all_steps = list(session.scalars(select(JobStep).where(JobStep.job_id == job.job_id)))
    assert len(all_steps) == 7 and all(step.status is StepStatus.COMPLETED for step in all_steps)
    assert len(forklift.execute_transport_requests) == 4  # inner+outer material, then two returns
    routes = [(row["pickup_code"], row["dropoff_code"]) for row in forklift.execute_transport_requests]
    assert routes == [("RACK1", "DROP"), ("DROP", "RACK1"), ("RACK2", "DROP"), ("DROP", "RACK2")]
    assert len(cell.commands) == 7
    return_home_attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.job_id == job.job_id,
        ExecutionAttempt.command_type == "RETURN_HOME",
    )))
    assert len(forklift.return_home_requests) == len(return_home_attempts) == 1
    assert return_home_attempts[0].status is ExecutionAttemptStatus.SUCCEEDED
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt).where(
        ExecutionAttempt.status.in_([
            __import__("shared.models.factory", fromlist=["ExecutionAttemptStatus"]).ExecutionAttemptStatus.CREATED,
            __import__("shared.models.factory", fromlist=["ExecutionAttemptStatus"]).ExecutionAttemptStatus.DISPATCHING,
            __import__("shared.models.factory", fromlist=["ExecutionAttemptStatus"]).ExecutionAttemptStatus.ACCEPTED,
        ])
    )) == 0


def test_auto_empty_return_waits_for_all_outer_steps_and_recovers_after_restart(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-AUTO-OUTER-RESTART")
    _pass_all_incoming_qa(session, job.job_id)
    outer = _delivery_by_group(session, job.job_id)["OUTER_WALLS"]
    _mark_material_arrived(
        session, job_id=job.job_id, delivery=outer, pickup_code="RACK1"
    )
    outer_step_ids = list(session.scalars(
        select(JobMaterialDeliveryItem.job_step_id)
        .where(JobMaterialDeliveryItem.job_delivery_id == outer.job_delivery_id)
        .order_by(JobMaterialDeliveryItem.delivery_item_id)
    ))
    assert len(outer_step_ids) == 4 and all(step_id is not None for step_id in outer_step_ids)
    worker, forklift, _ = _worker(session_factory)
    assert worker.tick() is True  # Existing Worker starts the REQUESTED Job.
    assert forklift.execute_transport_requests == []
    assert DropResourceService(session).get_drop_state().state is DropResourceState.OCCUPIED

    # OUTER is one pallet consumed by four distinct server-traceable Steps.
    # 1/4, 2/4, and 3/4 are each insufficient for an automatic return.
    for step_id in outer_step_ids[:3]:
        session.get(JobStep, step_id).status = StepStatus.COMPLETED
        session.commit()
        worker.tick()
        assert forklift.execute_transport_requests == []

    # This commits the fourth consuming Step, then simulates a process crash
    # before an in-memory callback could run. A fresh Worker derives eligibility
    # solely from Delivery, DeliveryItem, JobStep, Attempt, and DROP evidence.
    session.get(JobStep, outer_step_ids[3]).status = StepStatus.COMPLETED
    session.commit()
    fresh_worker, fresh_forklift, _ = _worker(session_factory)
    assert fresh_worker.tick() is True
    assert [(row["pickup_code"], row["dropoff_code"]) for row in fresh_forklift.execute_transport_requests] == [
        ("DROP", "RACK1")
    ]
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
    assert fresh_worker.tick() is False
    assert len(fresh_forklift.execute_transport_requests) == 1


def test_auto_empty_return_inner_failure_does_not_rollback_completed_step(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-AUTO-INNER-FAIL")
    _pass_all_incoming_qa(session, job.job_id)
    inner = _delivery_by_group(session, job.job_id)["INNER_WALL"]
    _mark_material_arrived(
        session, job_id=job.job_id, delivery=inner, pickup_code="RACK2"
    )
    inner_step_id = session.scalar(
        select(JobMaterialDeliveryItem.job_step_id).where(
            JobMaterialDeliveryItem.job_delivery_id == inner.job_delivery_id
        )
    )
    assert inner_step_id is not None
    session.get(JobStep, inner_step_id).status = StepStatus.COMPLETED
    session.commit()

    failed_return = ForkliftExecutionResult(
        status=ForkliftActionStatus.FAILED,
        error_code="FAKE_RETURN_FAILURE",
        detail="fake return failure",
    )
    worker, forklift, _ = _worker(session_factory, forklift_result=failed_return)
    assert worker.tick() is True  # Existing Worker starts the REQUESTED Job.
    session.expire_all()
    assert session.get(JobStep, inner_step_id).status is StepStatus.COMPLETED
    return_attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.job_delivery_id == inner.job_delivery_id,
        ExecutionAttempt.command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN",
    )))
    assert len(return_attempts) == 1
    assert return_attempts[0].status is ExecutionAttemptStatus.FAILED
    assert DropResourceService(session).get_drop_state().state is DropResourceState.OCCUPIED
    # A failed physical return is ambiguous: automatic reconciliation never retries it.
    assert worker.tick() is False
    assert len(forklift.execute_transport_requests) == 1


def test_auto_empty_return_excludes_manual_base_and_roof(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-AUTO-MANUAL-EXCLUDED")
    deliveries = _delivery_by_group(session, job.job_id)
    for group in ("BASE", "ROOF"):
        deliveries[group].status = MaterialDeliveryStatus.COMPLETED
        item_ids = list(session.scalars(select(JobMaterialDeliveryItem.job_step_id).where(
            JobMaterialDeliveryItem.job_delivery_id == deliveries[group].job_delivery_id
        )))
        for step_id in item_ids:
            if step_id is not None:
                session.get(JobStep, step_id).status = StepStatus.COMPLETED
    session.commit()
    worker, forklift, _ = _worker(session_factory)
    assert worker.tick() is True  # Job start may occur, but no transport may dispatch.
    assert forklift.execute_transport_requests == []



def test_auto_empty_return_is_disabled_without_fake_runtime(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-AUTO-REAL-GUARD")
    inner = _delivery_by_group(session, job.job_id)["INNER_WALL"]
    _mark_material_arrived(
        session, job_id=job.job_id, delivery=inner, pickup_code="RACK2"
    )
    inner_step_id = session.scalar(
        select(JobMaterialDeliveryItem.job_step_id).where(
            JobMaterialDeliveryItem.job_delivery_id == inner.job_delivery_id
        )
    )
    assert inner_step_id is not None
    session.get(JobStep, inner_step_id).status = StepStatus.COMPLETED
    session.commit()

    worker, forklift, _ = _worker(
        session_factory, auto_empty_pallet_return_enabled=False
    )
    worker.tick()
    assert forklift.execute_transport_requests == []
    assert DropResourceService(session).get_drop_state().state is DropResourceState.OCCUPIED



def test_operator_empty_return_api_requires_explicit_confirmation_and_job_ownership(
    session: Session,
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-RETURN-API")
    delivery = _delivery_by_group(session, job.job_id)["INNER_WALL"]

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            url = f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}/empty-pallet-return"
            rejected = client.post(url, json={"confirmed_empty": False})
            assert rejected.status_code == 422
            assert "confirmed_empty" in rejected.json()["detail"]
            wrong_job = client.post(
                f"/production/jobs/999999/material-deliveries/{delivery.job_delivery_id}/empty-pallet-return",
                json={"confirmed_empty": True},
            )
            assert wrong_job.status_code == 404
    finally:
        app.dependency_overrides.clear()



def _assert_voice_matches_unity_process(
    session: Session, session_factory: sessionmaker[Session], job, expected_code: str
) -> None:
    voice = ProductionStatusQueryService(session).get_active_job_status()
    unity = ProductionSnapshotService(session_factory).get_job_status(job.job_id)
    assert voice is not None and unity is not None
    assert voice.process_stage_code == unity["process_stage_code"] == expected_code
    assert voice.current_step_name == unity["process_stage_display_name"]


def test_unity_current_stage_code_projects_house_b_lifecycle_and_snapshot_consistently(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """The Unity timeline is derived from durable lifecycle state, not events."""
    job = _job_with_house_b_master(session, "HOUSE-B-UNITY-STAGES")
    stages = UnityCurrentStageProjectionService(session)

    # The request is observable before a material inspection has been started.
    assert stages.derive(job=job) == "JOB_REQUESTED"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "COMMAND_RECEIVED"
    _assert_voice_matches_unity_process(session, session_factory, job, "COMMAND_RECEIVED")

    # The same shared resolver represents a started-but-unreleased QA gate.
    first_item = session.scalar(select(JobMaterialDeliveryItem).join(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id
    ).order_by(JobMaterialDeliveryItem.delivery_item_id))
    assert first_item is not None
    MaterialInspectionService().request_inspection(session, first_item.delivery_item_id)
    session.commit()
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "INCOMING_QA"
    _assert_voice_matches_unity_process(session, session_factory, job, "INCOMING_QA")

    _pass_all_incoming_qa(session, job.job_id)
    ProductionOrchestrationService(session).start_job(job.job_id)
    session.refresh(job)
    assert stages.derive(job=job) == "INSTALL_BASE"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "BASE_INSTALL"
    _assert_voice_matches_unity_process(session, session_factory, job, "BASE_INSTALL")

    deliveries = _delivery_by_group(session, job.job_id)
    steps = list(session.scalars(
        select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)
    ))
    base = next(step for step in steps if step.operation_code == "INSTALL_BASE")
    inner = next(step for step in steps if step.operation_code == "INSTALL_INNER_WALL")
    outer_steps = [step for step in steps if "OUTER_WALL" in step.operation_code]
    assert len(outer_steps) == 4

    base.status = StepStatus.COMPLETED
    session.commit()
    assert stages.derive(job=job) == "DELIVER_OUTER_WALL"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "OUTER_WALL_DELIVERY"
    _assert_voice_matches_unity_process(session, session_factory, job, "OUTER_WALL_DELIVERY")

    _mark_material_arrived(
        session, job_id=job.job_id, delivery=deliveries["OUTER_WALLS"], pickup_code="RACK1"
    )
    assert stages.derive(job=job) == "INSTALL_OUTER_WALL"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "OUTER_WALL_INSTALL"
    _assert_voice_matches_unity_process(session, session_factory, job, "OUTER_WALL_INSTALL")
    for step in outer_steps:
        step.status = StepStatus.COMPLETED
    session.commit()
    assert stages.derive(job=job) == "RETURN_OUTER_PALLET"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "OUTER_RETURN_INNER_DELIVERY"
    _assert_voice_matches_unity_process(session, session_factory, job, "OUTER_RETURN_INNER_DELIVERY")

    session.add(
        ExecutionAttempt(
            req_id="outer-return-for-unity-stage",
            executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT_EMPTY_RETURN",
            job_id=job.job_id,
            job_delivery_id=deliveries["OUTER_WALLS"].job_delivery_id,
            attempt_no=2,
            status=ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK1"}),
        )
    )
    session.commit()
    assert stages.derive(job=job) == "DELIVER_INNER_WALL"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "OUTER_RETURN_INNER_DELIVERY"
    _assert_voice_matches_unity_process(session, session_factory, job, "OUTER_RETURN_INNER_DELIVERY")

    _mark_material_arrived(
        session, job_id=job.job_id, delivery=deliveries["INNER_WALL"], pickup_code="RACK2"
    )
    assert stages.derive(job=job) == "INSTALL_INNER_WALL"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "INNER_WALL_INSTALL"
    _assert_voice_matches_unity_process(session, session_factory, job, "INNER_WALL_INSTALL")
    inner.status = StepStatus.COMPLETED
    session.commit()
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "INNER_WALL_RETURN"
    _assert_voice_matches_unity_process(session, session_factory, job, "INNER_WALL_RETURN")
    session.add(
        ExecutionAttempt(
            req_id="inner-return-for-process-stage", executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT_EMPTY_RETURN", job_id=job.job_id,
            job_delivery_id=deliveries["INNER_WALL"].job_delivery_id, attempt_no=2,
            status=ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
        )
    )
    job.status = JobStatus.PRE_ROOF_READY
    session.add(
        ProductionInspection(
            production_job_id=job.job_id,
            inspection_type=ProductionInspectionType.PRE_ROOF,
            status=ProductionInspectionStatus.PENDING,
            is_passed=None,
        )
    )
    session.commit()
    assert stages.derive(job=job) == "PRE_ROOF_INSPECTION"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "PRE_ROOF_INSPECTION"
    _assert_voice_matches_unity_process(session, session_factory, job, "PRE_ROOF_INSPECTION")

    inspection = session.scalar(select(ProductionInspection).where(
        ProductionInspection.production_job_id == job.job_id,
        ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
    ))
    assert inspection is not None
    inspection.status = ProductionInspectionStatus.COMPLETED
    inspection.result = ProductionInspectionResultCode.FAIL
    inspection.production_valid = False
    inspection.is_passed = False
    session.commit()
    assert stages.derive(job=job) == "PRE_ROOF_INSPECTION"

    inspection.status = ProductionInspectionStatus.PENDING
    inspection.result = None
    inspection.production_valid = False
    inspection.is_passed = None
    session.commit()
    completion = ProductionCompletionService(session)
    completion.start_pre_roof_inspection(production_job_id=job.job_id)
    roof = completion.pass_pre_roof_inspection(production_job_id=job.job_id)
    session.refresh(job)
    assert stages.derive(job=job) == "INSTALL_ROOF"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "ROOF_INSTALL"
    _assert_voice_matches_unity_process(session, session_factory, job, "ROOF_INSTALL")

    roof.status = StepStatus.COMPLETED
    job.status = JobStatus.ROOF_READY
    session.commit()
    assert stages.derive(job=job) == "INSTALL_ROOF"
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "HOUSE_OUTBOUND"
    _assert_voice_matches_unity_process(session, session_factory, job, "HOUSE_OUTBOUND")

    job.status = JobStatus.COMPLETED
    job.completed_at = datetime.now(timezone.utc)
    session.commit()
    assert stages.derive_process_stage(job=job)["process_stage_code"] == "COMPLETED"
    _assert_voice_matches_unity_process(session, session_factory, job, "COMPLETED")

    # A reconnect snapshot and a live production_status projection use the
    # exact same helper and therefore recover the same stage.
    snapshot_service = ProductionSnapshotService(session_factory)
    live = snapshot_service.get_job_status(job.job_id)
    snapshot = snapshot_service.get_snapshot()["jobs"]
    restored = next(item for item in snapshot if item["job_id"] == job.job_id)
    assert live is not None
    assert live["current_stage_code"] == restored["current_stage_code"] == "INSTALL_ROOF"
    for field in ("job_id", "job_code", "product_code", "status", "current_step", "next_step"):
        assert field in live
    for forbidden in ("current_operation", "current_stage_index", "current_stage_name"):
        assert forbidden not in live


def test_unity_current_stage_code_is_null_for_failed_and_canceled_jobs(session: Session) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-UNITY-TERMINAL-STAGES")
    stages = UnityCurrentStageProjectionService(session)
    job.status = JobStatus.FAILED
    session.commit()
    assert stages.derive(job=job) is None
    job.status = JobStatus.CANCELED
    session.commit()
    assert stages.derive(job=job) is None


def test_one_ahead_restart_with_active_base_and_inner_transport_does_not_redispatch(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _job_with_house_b_master(session, "HOUSE-B-PREFETCH-RESTART")
    _pass_all_incoming_qa(session, job.job_id)
    deliveries = _delivery_by_group(session, job.job_id)
    base_step = session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id,
        JobStep.operation_code == "INSTALL_BASE",
    ))
    assert base_step is not None

    # Durable state after both physical commands were claimed, before either
    # terminal callback. The Cell attempt is ambiguous after restart, so a
    # new Worker must not replay it and must fail the existing JobStep closed.
    job.status = JobStatus.RUNNING
    base_step.status = StepStatus.RUNNING
    inner = deliveries["INNER_WALL"]
    inner.status = MaterialDeliveryStatus.IN_PROGRESS
    cell_attempt = ExecutionAttempt(
        req_id="prefetch-restart-base",
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="INSTALL_BASE",
        job_id=job.job_id,
        job_step_id=base_step.job_step_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED,
        request_payload_json="{}",
    )
    session.add(cell_attempt)
    session.add(ExecutionAttempt(
        req_id="prefetch-restart-inner",
        executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT",
        job_id=job.job_id,
        job_delivery_id=inner.job_delivery_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.DISPATCHING,
        request_payload_json="{}",
    ))
    session.commit()

    restarted_worker, forklift, cell = _worker(
        session_factory, material_prefetch_mode=MaterialPrefetchMode.ONE_AHEAD
    )
    assert restarted_worker.tick() is True
    session.refresh(job)
    session.refresh(base_step)
    session.refresh(cell_attempt)
    assert job.status is JobStatus.FAILED
    assert base_step.status is StepStatus.FAILED
    assert cell_attempt.status is ExecutionAttemptStatus.UNKNOWN
    assert cell.commands == []
    assert forklift.execute_transport_requests == []
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt).where(
        ExecutionAttempt.job_delivery_id == inner.job_delivery_id,
        ExecutionAttempt.command_type == "EXECUTE_TRANSPORT",
    )) == 1
