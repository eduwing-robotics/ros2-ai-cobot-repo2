from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from shared.models.factory import (
    Base,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialFeedStatus,
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
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.operator_execution_ready_service import OperatorExecutionReadyService
from tests.recipe_test_support import seed_and_reserve_inventory_for_job
from shared.services.physical_ready_service import PhysicalReadyService
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


@pytest.fixture(name="session")
def session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


def _new_delivery(
    session: Session,
    *,
    mode: SupplyMode | None,
    group: str | None,
    delivery_status: MaterialDeliveryStatus = MaterialDeliveryStatus.PENDING,
    qa: str = "INCOMPLETE",
    physical_ready: bool = False,
    suffix: str = "A",
    job: ProductionJob | None = None,
    step_order: int = 1,
    operation_code: str = "INSTALL_LEFT_OUTER_WALL",
    vision_class: str = "wall_ext_left",
) -> tuple[ProductionJob, JobStep, JobMaterialDelivery]:
    if job is None:
        product = Product(product_code=f"P3A_PRODUCT_{suffix}", product_name=f"P3A {suffix}")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=f"P3A_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
    part = Part(
        part_code=f"P3A_PART_{suffix}_{step_order}",
        part_name=f"P3A part {suffix}",
        category=PartCategory.STRUCTURE,
        vision_class=vision_class,
        unit="EA",
    )
    session.add(part)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=step_order,
        operation_code=operation_code,
        display_name=f"Install {suffix}",
        part_code=part.part_code,
        quantity=1,
        slot_code=f"TEST_SLOT_{suffix}_{step_order}",
        pick_zone="TEST_ZONE",
        vision_class=part.vision_class,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED and group in {"OUTER_WALLS", "INNER_WALL"} else None,
        is_terminal=True,
        status=StepStatus.PENDING,
    )
    session.add(step)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=step_order,
        delivery_code=f"P3A_DEL_{suffix}_{step_order}",
        display_name=f"P3A Delivery {suffix}",
        status=delivery_status,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED and group in {"OUTER_WALLS", "INNER_WALL"} else None,
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
    if qa != "INCOMPLETE":
        session.add(
            MaterialInspection(
                inspection_request_id=f"p3a-qa-{suffix}-{step_order}",
                delivery_item_id=item.delivery_item_id,
                inspection_cycle=1,
                status=MaterialInspectionStatus.COMPLETED,
                result=MaterialInspectionResult.PASS if qa == "PASS" else MaterialInspectionResult.FAIL,
                expected_part_code=part.part_code,
                expected_class_name=part.vision_class,
                expected_quantity=1,
                detected_quantity=1,
                production_valid=(qa == "PASS"),
            )
        )
    session.commit()
    if physical_ready:
        # First confirmation is intentionally legal only before transport starts.
        if delivery_status is not MaterialDeliveryStatus.PENDING:
            delivery.status = MaterialDeliveryStatus.PENDING
            session.commit()
        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=delivery.job_delivery_id,
            request_id=f"p3a-ready-{suffix}-{step_order}",
        )
        if delivery_status is not MaterialDeliveryStatus.PENDING:
            delivery.status = delivery_status
            session.commit()
    return job, step, delivery


def _readiness(session: Session, job: ProductionJob, step: JobStep):
    return StepReadinessService(MaterialDeliveryService(session)).evaluate(
        job_id=job.job_id,
        job_step_id=step.job_step_id,
    )


def _worker(session_factory: sessionmaker[Session], *, cell_transport: FakeCellActionTransport, forklift_transport: FakeForkliftActionTransport) -> FmsWorker:
    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(forklift_transport)

    def fms_factory(db: Session) -> FmsExecutionCoordinator:
        return FmsExecutionCoordinator(
            db,
            orchestration_service=ProductionOrchestrationService(db),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(db)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(db),
        )

    def forklift_factory(db: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(db, adapter=forklift_adapter, execution_attempt_service=ExecutionAttemptService(db))

    def feed_factory(db: Session) -> MaterialFeedExecutionCoordinator:
        return MaterialFeedExecutionCoordinator(db, robot_cell_adapter=cell_adapter)

    return FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_factory,
        forklift_execution_coordinator_factory=forklift_factory,
        material_feed_execution_coordinator_factory=feed_factory,
    )


def test_transported_readiness_requires_qa_ready_and_completed_transport(session: Session) -> None:
    job, step, delivery = _new_delivery(session, mode=SupplyMode.TRANSPORTED, group="TEST_TRANSPORT", suffix="FLOW")
    assert _readiness(session, job, step).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE

    item = delivery.items[0]
    session.add(
        MaterialInspection(
            inspection_request_id="p3a-flow-pass",
            delivery_item_id=item.delivery_item_id,
            inspection_cycle=1,
            status=MaterialInspectionStatus.COMPLETED,
            result=MaterialInspectionResult.PASS,
            expected_part_code=item.part_code,
            expected_class_name="wall_ext_left",
            expected_quantity=1,
            detected_quantity=1,
            production_valid=True,
        )
    )
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.PHYSICAL_READY_REQUIRED

    PhysicalReadyService(session).confirm_physical_ready(job_delivery_id=delivery.job_delivery_id, request_id="p3a-flow-ready")
    assert _readiness(session, job, step).reason is StepReadinessReason.TRANSPORT_PENDING
    delivery.status = MaterialDeliveryStatus.IN_PROGRESS
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.TRANSPORT_IN_PROGRESS
    delivery.status = MaterialDeliveryStatus.FAILED
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.TRANSPORT_FAILED
    delivery.status = MaterialDeliveryStatus.COMPLETED
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
    OperatorExecutionReadyService(session).confirm(job_id=job.job_id, job_step_id=step.job_step_id)
    assert _readiness(session, job, step).ready is True


def test_policy_deliveries_create_no_feed_but_legacy_keeps_feed(session: Session) -> None:
    policy_job, _, policy_delivery = _new_delivery(session, mode=SupplyMode.TRANSPORTED, group="TEST_FEED_POLICY", suffix="POLICY")
    manual_job, _, manual_delivery = _new_delivery(session, mode=SupplyMode.MANUAL, group="TEST_FEED_MANUAL", suffix="MANUAL")
    legacy_job, _, legacy_delivery = _new_delivery(session, mode=None, group=None, suffix="LEGACY")
    # Direct synthetic policy rows have no Feed. MaterialDeliveryService policy
    # creation is separately checked through materialized JobSteps below.
    assert session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == 0

    legacy_feed = JobMaterialFeedExecution(
        job_delivery_id=legacy_delivery.job_delivery_id,
        status=MaterialFeedStatus.PENDING,
        completed_json="[]",
    )
    session.add(legacy_feed)
    session.commit()
    assert session.get(JobMaterialFeedExecution, legacy_feed.feed_execution_id) is not None
    assert policy_job.job_id and manual_job.job_id and legacy_job.job_id


def test_stale_policy_feed_is_ignored_and_manual_is_fail_closed(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    job, step, delivery = _new_delivery(
        session,
        mode=SupplyMode.TRANSPORTED,
        group="TEST_STALE",
        delivery_status=MaterialDeliveryStatus.COMPLETED,
        qa="PASS",
        physical_ready=True,
        suffix="STALE",
    )
    stale = JobMaterialFeedExecution(
        job_delivery_id=delivery.job_delivery_id,
        status=MaterialFeedStatus.PENDING,
        completed_json="[]",
    )
    session.add(stale)
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
    OperatorExecutionReadyService(session).confirm(job_id=job.job_id, job_step_id=step.job_step_id)
    seed_and_reserve_inventory_for_job(session, job=job)
    session.commit()
    assert _readiness(session, job, step).ready is True
    seed_and_reserve_inventory_for_job(session, job=job)
    session.commit()
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    forklift = FakeForkliftActionTransport()
    assert _worker(session_factory, cell_transport=cell, forklift_transport=forklift).tick() is True
    assert len(cell.commands) == 1
    assert all(command.task_type != "MATERIAL_FEED" for command in cell.commands)
    assert not forklift.execute_transport_requests

    manual_job, manual_step, manual_delivery = _new_delivery(
        session,
        mode=SupplyMode.MANUAL,
        group="TEST_MANUAL_PENDING",
        qa="PASS",
        suffix="MANUALHOLD",
    )
    assert _readiness(session, manual_job, manual_step).reason is StepReadinessReason.MANUAL_PRESTAGE_REQUIRED
    assert manual_delivery.physical_ready_at is None


def test_manual_worker_is_fail_closed_with_zero_transport_feed_and_cell_actions(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    pending_job, pending_step, _ = _new_delivery(
        session,
        mode=SupplyMode.MANUAL,
        group="TEST_MANUAL_NOT_READY",
        qa="PASS",
        physical_ready=False,
        suffix="MANUALNOTREADY",
    )
    assert _readiness(session, pending_job, pending_step).reason is StepReadinessReason.MANUAL_PRESTAGE_REQUIRED

    _new_delivery(
        session,
        mode=SupplyMode.MANUAL,
        group="TEST_MANUAL_WORKER",
        qa="PASS",
        suffix="MANUALWORKER",
    )
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    forklift = FakeForkliftActionTransport()
    assert _worker(session_factory, cell_transport=cell, forklift_transport=forklift).tick() is False
    assert not forklift.execute_transport_requests
    assert not cell.commands


def test_manual_base_qa_and_prestage_dispatches_robot_cell_without_transport_or_feed(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    job, step, delivery = _new_delivery(
        session,
        mode=SupplyMode.MANUAL,
        group="TEST_MANUAL_BASE",
        qa="PASS",
        suffix="MANUALBASE",
        operation_code="INSTALL_BASE",
        vision_class="base_house_b",
    )
    assert _readiness(session, job, step).reason is StepReadinessReason.MANUAL_PRESTAGE_REQUIRED
    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id,
        request_id="manual-base-prestage",
    )
    assert _readiness(session, job, step).ready is True
    seed_and_reserve_inventory_for_job(session, job=job)
    session.commit()
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    forklift = FakeForkliftActionTransport()
    assert _worker(session_factory, cell_transport=cell, forklift_transport=forklift).tick() is True
    assert len(cell.commands) == 1
    assert cell.commands[0].task_type == "INSTALL_FLOOR"
    assert not forklift.execute_transport_requests
    assert session.scalar(
        select(JobMaterialFeedExecution).where(
            JobMaterialFeedExecution.job_delivery_id == delivery.job_delivery_id
        )
    ) is None


def test_policy_missing_delivery_never_uses_legacy_ready_shortcut(session: Session) -> None:
    product = Product(product_code="P3A_MISSING_PRODUCT", product_name="P3A missing")
    session.add(product)
    session.flush()
    job = ProductionJob(job_code="P3A_MISSING_JOB", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_LEFT_OUTER_WALL",
        display_name="Missing delivery",
        part_code="MISSING_PART",
        quantity=1,
        supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="TEST_MISSING",
        status=StepStatus.PENDING,
    )
    manual_step = JobStep(
        job_id=job.job_id,
        step_order=2,
        operation_code="INSTALL_LEFT_OUTER_WALL",
        display_name="Missing manual delivery",
        part_code="MISSING_PART_2",
        quantity=1,
        supply_mode=SupplyMode.MANUAL,
        supply_group_code="TEST_MISSING_MANUAL",
        status=StepStatus.PENDING,
    )
    session.add_all((step, manual_step))
    session.commit()
    assert _readiness(session, job, step).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
    assert _readiness(session, job, manual_step).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE


def test_multi_group_readiness_is_independent(session: Session) -> None:
    product = Product(product_code="P3A_MULTI_PRODUCT", product_name="P3A multi")
    session.add(product)
    session.flush()
    job = ProductionJob(job_code="P3A_MULTI_JOB", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.commit()
    _, outer_step, _ = _new_delivery(
        session,
        mode=SupplyMode.TRANSPORTED,
        group="TEST_OUTER",
        delivery_status=MaterialDeliveryStatus.COMPLETED,
        qa="PASS",
        physical_ready=True,
        suffix="OUTER",
        job=job,
        step_order=1,
    )
    _, inner_step, _ = _new_delivery(
        session,
        mode=SupplyMode.TRANSPORTED,
        group="TEST_INNER",
        delivery_status=MaterialDeliveryStatus.PENDING,
        qa="PASS",
        physical_ready=True,
        suffix="INNER",
        job=job,
        step_order=2,
    )
    assert _readiness(session, job, outer_step).reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
    OperatorExecutionReadyService(session).confirm(job_id=job.job_id, job_step_id=outer_step.job_step_id)
    assert _readiness(session, job, outer_step).ready is True
    assert _readiness(session, job, inner_step).reason is StepReadinessReason.TRANSPORT_PENDING


def test_worker_fake_e2e_transported_skips_feed_then_dispatches_cell(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    job, step, delivery = _new_delivery(session, mode=SupplyMode.TRANSPORTED, group="OUTER_WALLS", suffix="E2E")
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    forklift = FakeForkliftActionTransport()
    seed_and_reserve_inventory_for_job(session, job=job)
    session.commit()
    worker = _worker(session_factory, cell_transport=cell, forklift_transport=forklift)

    assert worker.tick() is False
    assert not forklift.execute_transport_requests and not cell.commands

    item = delivery.items[0]
    session.add(
        MaterialInspection(
            inspection_request_id="p3a-e2e-pass",
            delivery_item_id=item.delivery_item_id,
            inspection_cycle=1,
            status=MaterialInspectionStatus.COMPLETED,
            result=MaterialInspectionResult.PASS,
            expected_part_code=item.part_code,
            expected_class_name="wall_ext_left",
            expected_quantity=1,
            detected_quantity=1,
            production_valid=True,
        )
    )
    session.commit()
    assert worker.tick() is False
    assert not forklift.execute_transport_requests and not cell.commands

    PhysicalReadyService(session).confirm_physical_ready(job_delivery_id=delivery.job_delivery_id, request_id="p3a-e2e-ready")
    assert worker.tick() is True
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    assert len(forklift.execute_transport_requests) == 1
    assert not cell.commands
    assert session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == 0

    assert _readiness(session, job, step).reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
    OperatorExecutionReadyService(session).confirm(job_id=job.job_id, job_step_id=step.job_step_id)
    assert worker.tick() is True
    assert len(cell.commands) == 1
    assert cell.commands[0].task_type == "INSTALL_OUTER_WALL"
    assert all(command.task_type != "MATERIAL_FEED" for command in cell.commands)
    session.refresh(step)
    assert step.status is StepStatus.COMPLETED
