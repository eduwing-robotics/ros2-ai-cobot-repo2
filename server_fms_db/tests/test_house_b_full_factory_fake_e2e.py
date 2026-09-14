"""Server-side HOUSE_B golden path through fake Vision and fake Robot Cell.

This deliberately joins the two independently-tested server boundaries without
opening either PostgreSQL database or contacting physical equipment.  Incoming
QA uses the v0.2 UDP runtime against the loopback-only Fake Vision peer; Cell
work uses the ordinary FMS worker and its fake Cell transport.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from api_server.main import app
from api_server.routers.inventory import get_db
from fms_server.cell_action_transport import CELL_ACTION_CONTRACT_VERSION, CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.incoming_material_qa_udp_transport import IncomingQAUdpRuntime, IncomingQAUdpRuntimeConfig
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_slot_mapper import RobotCellSlotMapper
from fms_server.robot_cell_action_adapter import (
    RobotCellActionAdapter,
    RobotCellPartClassMapper,
    RobotCellTaskTypeMapper,
)
from fms_server.worker import FmsWorker
from scripts.fake_vision_incoming_qa_v02 import (
    FakeVisionIncomingQAConfig,
    FakeVisionIncomingQASimulator,
    FakeVisionScenario,
)
from scripts.seed_house_b_mvp_master import (
    HOUSE_B_PARTS,
    HOUSE_B_PRODUCT_CODE,
    HOUSE_B_STAGES,
    seed_house_b_mvp_master,
)
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    IncomingQATransaction,
    Inventory,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobStep,
    JobStatus,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    ProductionInspection,
    ProductionInspectionStatus,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_v02_monitoring_service import IncomingQAV02MonitoringService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.operator_execution_ready_service import (
    OperatorExecutionReadyService,
    operator_execution_ready_required,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService


_VISION_CLASS_BY_PART = {part_code: vision_class for part_code, _name, vision_class in HOUSE_B_PARTS}


@pytest.fixture(name="session_factory")
def session_factory_fixture(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Isolated SQLite file supports concurrent UDP handler/read sessions."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'house_b_full_factory_fake.sqlite'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

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


@pytest.fixture(name="client")
def client_fixture(session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


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
            select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job_id)
        )
    }


def _make_worker(
    session_factory: sessionmaker[Session], *, cell: FakeCellActionTransport
) -> FmsWorker:
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


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        return int(reserve.getsockname()[1])


async def _wait_for_transaction(
    factory: sessionmaker[Session], transaction_id: int, status: IncomingQATransactionStatus
) -> None:
    for _ in range(150):
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            if transaction is not None and transaction.status is status:
                if status is not IncomingQATransactionStatus.COMPLETED:
                    return
                active_items = session.scalar(
                    select(func.count()).select_from(MaterialInspection).where(
                        MaterialInspection.incoming_qa_transaction_id == transaction_id,
                        MaterialInspection.status.in_(
                            (MaterialInspectionStatus.REQUESTED, MaterialInspectionStatus.RUNNING)
                        ),
                    )
                )
                if active_items == 0:
                    return
        await asyncio.sleep(0.01)
    raise AssertionError(f"transaction={transaction_id} did not reach {status.value}")


async def _wait_for_no_active_incoming_qa_transaction(factory: sessionmaker[Session]) -> None:
    """Wait for UDP handler tasks to finish before planning the next request."""
    active = (
        IncomingQATransactionStatus.REQUESTED,
        IncomingQATransactionStatus.SENT,
        IncomingQATransactionStatus.ACKED,
    )
    for _ in range(150):
        with factory() as session:
            remaining = session.scalar(
                select(func.count())
                .select_from(IncomingQATransaction)
                .where(IncomingQATransaction.status.in_(active))
            )
            if remaining == 0:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("Incoming QA handler left an active transaction after terminal result.")


def _run_initial_incoming_qa_over_loopback(
    factory: sessionmaker[Session], *, job_id: int
) -> tuple[list[tuple[str, int, tuple[str, ...]]], list[tuple[str, int]], list[tuple[str, int]]]:
    """Exercise request -> ACK source port -> fixed-port result for both modes."""

    async def exercise() -> tuple[list[tuple[str, int, tuple[str, ...]]], list[tuple[str, int]], list[tuple[str, int]]]:
        result_port = _free_udp_port()
        fake = FakeVisionIncomingQASimulator(
            FakeVisionIncomingQAConfig(
                host="127.0.0.1",
                request_port=0,
                server_result_host="127.0.0.1",
                server_result_port=result_port,
                scenario=FakeVisionScenario.ALL_PASS,
                # This golden path asserts normal ACK -> Result sequencing.
                # Dedicated UDP tests cover deliberately reversed packet/task order;
                # a longer delay avoids StaticPool's single-connection scheduling
                # artifact from making this integration scenario nondeterministic.
                result_delay_seconds=0.15,
            )
        )
        await fake.start()
        assert fake.bound_port is not None
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1",
                vision_port=fake.bound_port,
                result_host="127.0.0.1",
                result_port=result_port,
                ack_timeout_seconds=0.1,
                max_retries=1,
            ),
        )
        await runtime.start()
        try:
            with factory() as session:
                initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
                assert initial.send_transaction_id is not None
                base_id = initial.send_transaction_id
            await runtime.send_transaction(base_id)
            await _wait_for_transaction(factory, base_id, IncomingQATransactionStatus.COMPLETED)
            await _wait_for_no_active_incoming_qa_transaction(factory)

            with factory() as session:
                followup = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
                assert followup.send_transaction_id is not None
                house_id = followup.send_transaction_id
            await runtime.send_transaction(house_id)
            await _wait_for_transaction(factory, house_id, IncomingQATransactionStatus.COMPLETED)
            return (
                [(entry.inspection_mode, entry.inspection_cycle, entry.slots) for entry in fake.requests],
                fake.ack_destinations,
                fake.result_destinations,
            )
        finally:
            await runtime.close()
            await fake.close()

    return asyncio.run(exercise())


def _step_by_order(session: Session, *, job_id: int, order: int) -> JobStep:
    step = session.scalar(select(JobStep).where(JobStep.job_id == job_id, JobStep.step_order == order))
    assert step is not None
    return step


def _assert_dispatch_matches_snapshot(command, *, job_id: int, step: JobStep) -> None:
    assert command.ver == CELL_ACTION_CONTRACT_VERSION == "0.2.1"
    assert command.job_id == str(job_id)
    assert command.step_id == str(step.job_step_id)
    assert command.product == HOUSE_B_PRODUCT_CODE
    assert command.task_type == RobotCellTaskTypeMapper.map_operation_code(step.operation_code)
    assert json.loads(command.parts_json) == [{
        "slot": RobotCellSlotMapper.map(
            product_code=HOUSE_B_PRODUCT_CODE, canonical_slot_code=step.slot_code, part_code=step.part_code
        ),
        "class": RobotCellPartClassMapper.map_operation_code(step.operation_code),
        "part_code": step.part_code,
        **({"zone": step.pick_zone} if step.pick_zone is not None else {}),
    }]
    assert step.vision_class == _VISION_CLASS_BY_PART[step.part_code]


def _assert_succeeded_attempt(session: Session, *, step: JobStep) -> None:
    attempt = session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.job_step_id == step.job_step_id,
            ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
        )
    )
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert attempt.dispatch_started_at is not None
    assert attempt.goal_accepted_at is not None and attempt.completed_at is not None


def _delivery_url(job_id: int, delivery_id: int, action: str) -> str:
    return f"/production/jobs/{job_id}/material-deliveries/{delivery_id}/{action}"



def _approve_next_transported_cell_step(session: Session, *, job_id: int) -> None:
    """Test-side operator command seam; it never changes Step status or dispatches."""

    step = ProductionOrchestrationService(session).get_next_step(job_id)
    if step is not None and operator_execution_ready_required(step):
        OperatorExecutionReadyService(session).confirm(
            job_id=job_id, job_step_id=step.job_step_id
        )


def test_house_b_full_factory_golden_path_through_fake_vision_and_cell(
    session: Session,
    session_factory: sessionmaker[Session],
    client: TestClient,
) -> None:
    job = _create_house_b_job(session, job_code="HOUSE-B-FULL-FACTORY-FAKE")
    deliveries = _deliveries_by_group(session, job_id=job.job_id)
    assert set(deliveries) == {"BASE", "INNER_WALL", "OUTER_WALLS", "ROOF"}

    # The operator-facing API, rather than a frontend-only convention, blocks
    # both manual and transported physical preparation while QA is HOLD.
    base_url = _delivery_url(job.job_id, deliveries["BASE"].job_delivery_id, "manual-prestage-ready")
    inner_url = _delivery_url(job.job_id, deliveries["INNER_WALL"].job_delivery_id, "physical-ready")
    assert client.post(base_url, json={"request_id": "before-qa-base"}).status_code == 409
    assert client.post(inner_url, json={"request_id": "before-qa-inner"}).status_code == 409
    session.rollback()

    blocked_cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    _make_worker(session_factory, cell=blocked_cell).tick()
    assert blocked_cell.commands == []

    request_log, ack_destinations, result_destinations = _run_initial_incoming_qa_over_loopback(
        session_factory, job_id=job.job_id
    )
    session.expire_all()
    transactions = list(session.scalars(
        select(IncomingQATransaction)
        .where(IncomingQATransaction.production_job_id == job.job_id)
        .order_by(IncomingQATransaction.transaction_id)
    ))
    assert [(tx.inspection_mode, tx.inspection_cycle, tx.status, tx.overall_result, tx.production_valid) for tx in transactions] == [
        ("BASE_AB", 1, IncomingQATransactionStatus.COMPLETED, MaterialInspectionResult.PASS, True),
        ("HOUSE_B", 1, IncomingQATransactionStatus.COMPLETED, MaterialInspectionResult.PASS, True),
    ]
    assert all(tx.ack_accepted is True and tx.acked_at is not None for tx in transactions)
    inspections = list(session.scalars(select(MaterialInspection).order_by(MaterialInspection.inspection_id)))
    assert len(inspections) == 7
    assert all(
        item.status is MaterialInspectionStatus.COMPLETED
        and item.result is MaterialInspectionResult.PASS
        and item.production_valid is True
        for item in inspections
    )
    readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id)
    assert readiness.ready is True and (readiness.released_items, readiness.total_items) == (7, 7)
    assert IncomingQAV02MonitoringService(session).get_job_monitor(job_id=job.job_id).gate.status == "RELEASE"
    assert request_log[0] == ("BASE_AB", 1, ("C09",))
    assert request_log[1][:2] == ("HOUSE_B", 1)
    assert set(request_log[1][2]) == {"B01", "B02", "B03", "B04", "B05", "B06"}
    assert len(ack_destinations) == len(result_destinations) == 2
    assert all(destination[0] == "127.0.0.1" for destination in ack_destinations + result_destinations)
    assert all(destination[1] != result_destinations[0][1] for destination in ack_destinations)

    # Release permits the existing operator/API handoff; no direct Delivery
    # status manipulation is used.
    assert client.post(base_url, json={"request_id": "after-qa-base"}).status_code == 200
    outer_url = _delivery_url(job.job_id, deliveries["OUTER_WALLS"].job_delivery_id, "physical-ready")
    assert client.post(inner_url, json={"request_id": "after-qa-inner"}).status_code == 200
    assert client.post(outer_url, json={"request_id": "after-qa-outer"}).status_code == 200
    delivery_service = MaterialDeliveryService(session)
    for group in ("INNER_WALL", "OUTER_WALLS"):
        delivery = deliveries[group]
        delivery_service.start_delivery(delivery.job_delivery_id)
        delivery_service.complete_delivery(delivery.job_delivery_id)

    cell = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(7)
    ])
    worker = _make_worker(session_factory, cell=cell)
    for expected_stage in HOUSE_B_STAGES[:6]:
        order, operation_code, _display, part_code, _slot, *_rest = expected_stage
        _approve_next_transported_cell_step(session, job_id=job.job_id)
        assert worker.tick() is True
        session.expire_all()
        step = _step_by_order(session, job_id=job.job_id, order=order)
        assert step.operation_code == operation_code and step.part_code == part_code
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
    assert job.status is JobStatus.PRE_ROOF_READY and len(cell.commands) == 6
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    assert session.scalar(select(func.count()).select_from(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    )) == 0
    assert worker.tick() is False and len(cell.commands) == 6

    completion = ProductionCompletionService(session)
    completion.start_pre_roof_inspection(production_job_id=job.job_id)
    roof_step = completion.pass_pre_roof_inspection(production_job_id=job.job_id)
    assert roof_step.is_terminal is True and roof_step.status is StepStatus.PENDING
    roof_url = _delivery_url(job.job_id, deliveries["ROOF"].job_delivery_id, "manual-prestage-ready")
    assert client.post(roof_url, json={"request_id": "after-qa-roof"}).status_code == 200

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


def test_house_b_qa_hold_blocks_physical_handoff_and_cell_dispatch(
    session: Session,
    session_factory: sessionmaker[Session],
    client: TestClient,
) -> None:
    """A minimal cross-boundary negative: no QA release means no handoff/work."""
    job = _create_house_b_job(session, job_code="HOUSE-B-FULL-FACTORY-QA-HOLD")
    deliveries = _deliveries_by_group(session, job_id=job.job_id)
    base_url = _delivery_url(job.job_id, deliveries["BASE"].job_delivery_id, "manual-prestage-ready")
    outer_url = _delivery_url(job.job_id, deliveries["OUTER_WALLS"].job_delivery_id, "physical-ready")
    assert client.post(base_url, json={"request_id": "hold-base"}).status_code == 409
    assert client.post(outer_url, json={"request_id": "hold-outer"}).status_code == 409
    session.rollback()
    cell = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
    _make_worker(session_factory, cell=cell).tick()
    assert cell.commands == []
