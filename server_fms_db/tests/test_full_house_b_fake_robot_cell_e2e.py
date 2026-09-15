"""Full HOUSE_B FMS-to-Fake-Cell stage lifecycle regression.

This test deliberately prepares durable upstream facts through the same
application services an operator/transport integration uses.  The actual
production work still traverses FmsWorker -> payload builder -> v0.2.1 Cell
adapter -> FakeCellActionTransport -> orchestration transitions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.cell_action_transport import CELL_ACTION_CONTRACT_VERSION, CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_slot_mapper import RobotCellSlotMapper
from fms_server.robot_cell_action_adapter import (
    RobotCellActionAdapter,
    RobotCellPartClassMapper,
    RobotCellTaskTypeMapper,
)
from fms_server.worker import FmsWorker
from scripts.seed_house_b_mvp_master import (
    HOUSE_B_PARTS,
    HOUSE_B_PRODUCT_CODE,
    HOUSE_B_STAGES,
    seed_house_b_mvp_master,
)
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    Inventory,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStep,
    JobStatus,
    ProductionInspection,
    ProductionInspectionStatus,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.operator_execution_ready_service import (
    OperatorExecutionReadyService,
    operator_execution_ready_required,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.physical_ready_service import PhysicalReadyService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from tests.recipe_test_support import seed_complete_incoming_qa


_VISION_CLASS_BY_PART = {part_code: vision_class for part_code, _name, vision_class in HOUSE_B_PARTS}


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    """Isolated shared in-memory DB; never opens either PostgreSQL database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="session")
def session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as db:
        yield db


def _make_worker(
    session_factory: sessionmaker[Session], *, cell: FakeCellActionTransport
) -> FmsWorker:
    """Build the production Worker with its normal fake-only adapters."""
    cell_adapter = RobotCellActionAdapter(cell)
    forklift_adapter = ForkliftActionAdapter(FakeForkliftActionTransport())

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
        auto_empty_pallet_return_enabled=False,
    )


def _create_house_b_job(session: Session, *, job_code: str):
    seed_house_b_mvp_master(session)
    # Physical stock is operational data, not part of the production master
    # seed. Provide it explicitly for this isolated fake factory fixture.
    for part_code, _name, _vision_class in HOUSE_B_PARTS:
        if session.get(Inventory, part_code) is None:
            session.add(Inventory(part_code=part_code, quantity=100))
    session.commit()
    return ProductionOrchestrationService(session).create_job(
        product_code=HOUSE_B_PRODUCT_CODE,
        job_code=job_code,
        roof_option_code=RoofOptionCode.ROOF_02,
    )


def _deliveries_by_group(session: Session, *, job_id: int) -> dict[str, JobMaterialDelivery]:
    return {
        delivery.supply_group_code: delivery
        for delivery in session.scalars(
            select(JobMaterialDelivery).where(
                JobMaterialDelivery.production_job_id == job_id
            )
        )
    }


def _prepare_upstream_readiness(session: Session, *, job_id: int) -> dict[str, JobMaterialDelivery]:
    """Create valid QA evidence and physical facts through existing services.

    The test intentionally does not fake Cell work by changing JobStep status.
    Transports are pre-completed through the normal Delivery service because
    transport execution itself is outside this Cell-stage E2E's scope.
    """
    assert seed_complete_incoming_qa(session, job_id=job_id) == 7
    deliveries = _deliveries_by_group(session, job_id=job_id)
    assert set(deliveries) == {"BASE", "INNER_WALL", "OUTER_WALLS", "ROOF"}

    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job_id,
        job_delivery_id=deliveries["BASE"].job_delivery_id,
        request_id=f"golden-base-prestage-{job_id}",
    )
    delivery_service = MaterialDeliveryService(session)
    physical_ready = PhysicalReadyService(session)
    for group in ("INNER_WALL", "OUTER_WALLS"):
        delivery = deliveries[group]
        assert delivery.supply_mode is SupplyMode.TRANSPORTED
        physical_ready.confirm_physical_ready(
            job_delivery_id=delivery.job_delivery_id,
            request_id=f"golden-{group.lower()}-physical-ready-{job_id}",
        )
        delivery_service.start_delivery(delivery.job_delivery_id)
        delivery_service.complete_delivery(delivery.job_delivery_id)
    return deliveries


def _step_by_order(session: Session, *, job_id: int, order: int) -> JobStep:
    step = session.scalar(
        select(JobStep).where(JobStep.job_id == job_id, JobStep.step_order == order)
    )
    assert step is not None
    return step


def _assert_dispatch_matches_snapshot(command, *, job_id: int, step: JobStep) -> None:
    """Assert actual ExecuteTask v0.2.1 serialization against the JobStep snapshot."""
    assert command.ver == CELL_ACTION_CONTRACT_VERSION == "0.2.1"
    assert command.job_id == str(job_id)
    assert command.step_id == str(step.job_step_id)
    assert command.product == HOUSE_B_PRODUCT_CODE
    assert command.task_type == RobotCellTaskTypeMapper.map_operation_code(step.operation_code)

    parts = json.loads(command.parts_json)
    assert parts == [{
        "slot": RobotCellSlotMapper.map(
            product_code=HOUSE_B_PRODUCT_CODE, canonical_slot_code=step.slot_code, part_code=step.part_code
        ),
        "class": RobotCellPartClassMapper.map_operation_code(step.operation_code),
        "part_code": step.part_code,
        **({"zone": step.pick_zone} if step.pick_zone is not None else {}),
    }]
    # The detailed Vision class remains a JobStep snapshot; the ExecuteTask
    # payload correctly uses only the approved coarse manipulation class.
    assert step.vision_class == _VISION_CLASS_BY_PART[step.part_code]


def _assert_succeeded_attempt(session: Session, *, step: JobStep) -> None:
    attempt = session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.job_step_id == step.job_step_id,
            ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
        )
    )
    assert attempt is not None
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert attempt.dispatch_started_at is not None
    assert attempt.goal_accepted_at is not None and attempt.completed_at is not None



def _approve_next_transported_cell_step(session: Session, *, job_id: int) -> None:
    """Test-side operator command seam; it never changes Step status or dispatches."""

    step = ProductionOrchestrationService(session).get_next_step(job_id)
    if step is not None and operator_execution_ready_required(step):
        OperatorExecutionReadyService(session).confirm(
            job_id=job_id, job_step_id=step.job_step_id
        )


def test_house_b_full_stage_golden_path_through_fake_cell_and_pre_roof(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """Exercise all seven real FMS Cell dispatches around the PRE_ROOF boundary."""
    job = _create_house_b_job(session, job_code="HOUSE-B-FULL-CELL-GOLDEN")
    deliveries = _prepare_upstream_readiness(session, job_id=job.job_id)
    initial_steps = list(session.scalars(
        select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)
    ))
    assert len(initial_steps) == 6
    assert all(not step.is_terminal for step in initial_steps)
    assert session.scalar(select(func.count()).select_from(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) == 0

    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(7)
    ])
    worker = _make_worker(session_factory, cell=cell)

    # The exact current HOUSE_B recipe order is an E2E expectation, not a
    # production scheduler special case.
    for expected_stage in HOUSE_B_STAGES[:6]:
        order, operation_code, _display, part_code, _slot, *_rest = expected_stage
        _approve_next_transported_cell_step(session, job_id=job.job_id)
        assert worker.tick() is True
        session.expire_all()
        step = _step_by_order(session, job_id=job.job_id, order=order)
        assert step.operation_code == operation_code
        assert step.part_code == part_code
        assert step.status is StepStatus.COMPLETED
        assert step.started_at is not None and step.completed_at is not None
        _assert_succeeded_attempt(session, step=step)
        _assert_dispatch_matches_snapshot(cell.commands[-1], job_id=job.job_id, step=step)

    inner_delivery = next(delivery for delivery in deliveries.values() if delivery.supply_group_code == "INNER_WALL")
    session.add(ExecutionAttempt(
        req_id=f"inner-return-gate-{job.job_id}", executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT_EMPTY_RETURN", job_id=job.job_id,
        job_delivery_id=inner_delivery.job_delivery_id, attempt_no=99,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
    ))
    session.commit()
    ProductionOrchestrationService(session).try_enter_pre_roof_ready_after_inner_return(job.job_id)
    session.refresh(job)
    assert job.status is JobStatus.PRE_ROOF_READY
    assert len(cell.commands) == 6
    inspection = session.scalar(select(ProductionInspection).where(
        ProductionInspection.production_job_id == job.job_id
    ))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    # The generic execution gate prevents a runtime Roof step/dispatch until
    # the existing PRE_ROOF authority has accepted a PASS.
    assert session.scalar(select(func.count()).select_from(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) == 0
    assert worker.tick() is False
    assert len(cell.commands) == 6

    completion = ProductionCompletionService(session)
    completion.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = completion.pass_pre_roof_inspection(production_job_id=job.job_id)
    assert roof_step.is_terminal is True
    assert roof_step.status is StepStatus.PENDING
    ManualPrestageService(session).confirm_manual_prestage_ready(
        job_id=job.job_id,
        job_delivery_id=deliveries["ROOF"].job_delivery_id,
        request_id=f"golden-roof-prestage-{job.job_id}",
    )

    assert worker.tick() is True
    session.expire_all()
    roof_step = session.get(JobStep, roof_step.job_step_id)
    session.refresh(job)
    assert roof_step is not None and roof_step.status is StepStatus.COMPLETED
    assert job.status is JobStatus.ROOF_READY
    ProductionOrchestrationService(session).complete_job(job.job_id)
    session.refresh(job)
    assert job.status is JobStatus.COMPLETED and job.completed_at is not None
    _assert_succeeded_attempt(session, step=roof_step)
    _assert_dispatch_matches_snapshot(cell.commands[-1], job_id=job.job_id, step=roof_step)
    assert len(cell.commands) == 7
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt).where(
        ExecutionAttempt.job_id == job.job_id,
        ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
        ExecutionAttempt.status == ExecutionAttemptStatus.SUCCEEDED,
    )) == 7


def test_house_b_inner_cell_failure_stops_before_outer_or_pre_roof(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _create_house_b_job(session, job_code="HOUSE-B-FULL-CELL-INNER-FAIL")
    _prepare_upstream_readiness(session, job_id=job.job_id)
    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()),
        FakeCellActionExchange(CellTaskExecutionResult.cell_failed(
            error_code="FAKE_INNER_FAILURE", detail="deterministic inner failure"
        )),
    ])
    worker = _make_worker(session_factory, cell=cell)

    _approve_next_transported_cell_step(session, job_id=job.job_id)
    assert worker.tick() is True  # BASE
    _approve_next_transported_cell_step(session, job_id=job.job_id)
    assert worker.tick() is True  # INNER failure
    session.expire_all()
    base = _step_by_order(session, job_id=job.job_id, order=1)
    inner = _step_by_order(session, job_id=job.job_id, order=2)
    session.refresh(job)
    assert base.status is StepStatus.COMPLETED
    assert inner.status is StepStatus.FAILED
    assert job.status is JobStatus.FAILED
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.job_step_id == inner.job_step_id,
        ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
    ))
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.FAILED
    assert attempt.error_code == "FAKE_INNER_FAILURE"
    assert len(cell.commands) == 2
    assert all(_step_by_order(session, job_id=job.job_id, order=order).status is StepStatus.PENDING for order in range(3, 7))
    assert session.scalar(select(ProductionInspection).where(
        ProductionInspection.production_job_id == job.job_id
    )) is None
    assert worker.tick() is False
    assert len(cell.commands) == 2


def test_house_b_restart_after_completed_base_advances_only_inner(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = _create_house_b_job(session, job_code="HOUSE-B-FULL-CELL-RESTART")
    _prepare_upstream_readiness(session, job_id=job.job_id)
    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()),
        FakeCellActionExchange(CellTaskExecutionResult.success()),
    ])
    first_worker = _make_worker(session_factory, cell=cell)
    _approve_next_transported_cell_step(session, job_id=job.job_id)
    assert first_worker.tick() is True  # BASE completes durably.
    session.expire_all()
    base = _step_by_order(session, job_id=job.job_id, order=1)
    assert base.status is StepStatus.COMPLETED

    restarted_worker = _make_worker(session_factory, cell=cell)
    # Delivery completion persists across restart, but no release means a restart
    # must still not send the inner-wall Robot Cell action.
    assert restarted_worker.tick() is False
    assert len(cell.commands) == 1
    _approve_next_transported_cell_step(session, job_id=job.job_id)
    assert restarted_worker.tick() is True
    session.expire_all()
    inner = _step_by_order(session, job_id=job.job_id, order=2)
    assert base.status is StepStatus.COMPLETED
    assert inner.status is StepStatus.COMPLETED
    assert [command.step_id for command in cell.commands] == [
        str(base.job_step_id), str(inner.job_step_id)
    ]
