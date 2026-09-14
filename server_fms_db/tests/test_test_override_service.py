from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    Inventory,
    JobMaterialDeliveryItem,
    Part,
    PartCategory,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionInspectionType,
    EventType,
    IncomingQATransaction,
    IncomingQATransactionStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    ProductionJobControlState,
    StepStatus,
    SupplyMode,
)
from fms_server.operator_empty_pallet_return_service import OperatorEmptyPalletReturnService
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.services.drop_resource_service import DropResourceService, DropResourceState, DropResourceUnavailableError
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.test_override_service import (
    CurrentBlocker,
    CurrentBlockerType,
    TestOverrideDeniedError,
    TestOverrideService,
    TestOverrideStaleError,
)
from shared.realtime.incoming_qa_events import set_incoming_qa_change_callback
from shared.realtime.production_events import set_production_change_callback
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotService
from fms_server.worker import FmsWorker
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessResult, StepReadinessService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="OVERRIDE", product_name="Override"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _job(session: Session, *, control_state=ProductionJobControlState.ACTIVE):
    job = ProductionJob(product_code="OVERRIDE", job_code="OVERRIDE-1", status=JobStatus.RUNNING, control_state=control_state)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST", display_name="Test", status=StepStatus.PENDING, is_terminal=True)
    session.add(step)
    session.commit()
    return job, step


def _service(session: Session, *, database="smart_factory_benchmark", enabled=True):
    return TestOverrideService(session, enabled=enabled, database_name_resolver=lambda: database)


def _incoming_qa_job(session: Session) -> ProductionJob:
    session.add(Product(product_code="HOUSE_B", product_name="House B", is_active=True))
    session.flush()
    job = ProductionJob(job_code="OVERRIDE-QA", product_code="HOUSE_B", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="QA",
        display_name="Incoming QA", status=MaterialDeliveryStatus.PENDING,
    )
    session.add(delivery)
    session.flush()
    expected = (
        ("base_house_b", "QA-C09"),
        ("wall_ext_back_window", "QA-B01"),
        ("wall_ext_door", "QA-B02"),
        ("wall_ext_left_window", "QA-B03"),
        ("wall_ext_right", "QA-B04"),
        ("wall_int_house_b", "QA-B05"),
        ("roof_zip", "QA-B06"),
    )
    session.add_all([
        Part(part_code=part_code, part_name=part_code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA")
        for vision_class, part_code in expected
    ])
    session.flush()
    session.add_all([
        JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=part_code, quantity=1)
        for _vision_class, part_code in expected
    ])
    session.commit()
    return job


def test_guard_requires_explicit_enable_and_exact_benchmark(session: Session):
    job, _ = _job(session)
    with pytest.raises(TestOverrideDeniedError):
        _service(session, enabled=False).advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=1)
    for database in ("smart_factory_db", "smart_factory_rehearsal", "other"):
        with pytest.raises(TestOverrideDeniedError):
            _service(session, database=database).advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=1)


def test_requested_ready_robot_step_is_exposed_and_completed_without_fms(session: Session, monkeypatch):
    """FMS-off demos reuse normal start/step lifecycle instead of hiding a ready first step."""
    job, step = _job(session)
    job.status = JobStatus.REQUESTED
    step.is_terminal = False
    session.add(JobStep(
        job_id=job.job_id,
        step_order=2,
        operation_code="INSTALL_FOLLOWUP",
        display_name="Follow-up",
        status=StepStatus.PENDING,
        is_terminal=True,
    ))
    session.commit()
    monkeypatch.setattr(
        StepReadinessService,
        "evaluate",
        lambda _self, *, job_id, job_step_id: StepReadinessResult.ready_now(),
    )

    production_events: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: production_events.append((job_id, reason)))
    try:
        service = _service(session)
        blocker = service.current_blocker(job_id=job.job_id)
        assert blocker == CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id)

        result = service.advance(
            job_id=job.job_id,
            expected_blocker_type=blocker.blocker_type,
            expected_entity_id=blocker.entity_id,
        )

        assert result.advanced == blocker
        assert job.status is JobStatus.RUNNING
        assert step.status is StepStatus.COMPLETED
        assert session.query(ExecutionAttempt).filter_by(job_step_id=step.job_step_id).count() == 0
        assert [event.event_type for event in session.query(ProductionEvent).filter_by(job_id=job.job_id).order_by(ProductionEvent.event_id)] == [
            EventType.JOB_STARTED,
            EventType.STEP_STARTED,
            EventType.STEP_COMPLETED,
        ]
        assert production_events == [
            (job.job_id, "job_started"),
            (job.job_id, "step_started"),
            (job.job_id, "step_completed"),
        ]
    finally:
        set_production_change_callback(None)


def test_robot_step_uses_orchestration_start_then_complete_and_stale_click_is_rejected(session: Session, monkeypatch):
    job, step = _job(session)
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id))
    result = service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id)
    assert result.advanced.entity_id == step.job_step_id
    assert step.status is StepStatus.COMPLETED
    monkeypatch.undo()
    with pytest.raises(TestOverrideStaleError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id)


@pytest.mark.parametrize("control", [
    ProductionJobControlState.PAUSE_REQUESTED,
    ProductionJobControlState.PAUSED,
    ProductionJobControlState.RESUME_REQUESTED,
])
def test_non_active_pause_control_blocks_override(session: Session, control):
    job, step = _job(session, control_state=control)
    service = _service(session)
    with pytest.raises(TestOverrideDeniedError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id)


def test_unresolved_attempt_blocks_synthetic_robot_completion(session: Session, monkeypatch):
    job, step = _job(session)
    session.add(ExecutionAttempt(req_id="real", executor_type=ExecutorType.ROBOT_CELL, command_type="EXECUTE_TASK", job_id=job.job_id, job_step_id=step.job_step_id, attempt_no=1, status=ExecutionAttemptStatus.ACCEPTED, request_payload_json="{}"))
    session.commit()
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id))
    with pytest.raises(TestOverrideDeniedError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id)


def test_delivery_uses_transport_attempt_then_complete_transition_and_owns_drop(session: Session, monkeypatch):
    job, step = _job(session)
    part = Part(part_code="OVERRIDE-OUTER", part_name="Override outer", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part)
    session.flush()
    step.part_code = part.part_code
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="D", display_name="D",
        status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    session.add(JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id,
        part_code=part.part_code, quantity=1,
    ))
    session.commit()
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, delivery.job_delivery_id))
    service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=delivery.job_delivery_id)
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    attempt = session.query(ExecutionAttempt).filter_by(
        job_delivery_id=delivery.job_delivery_id, command_type="EXECUTE_TRANSPORT"
    ).one()
    assert attempt.executor_type is ExecutorType.FORKLIFT
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    drop = DropResourceService(session).get_drop_state()
    assert drop.state is DropResourceState.OCCUPIED
    assert drop.owner_delivery_id == delivery.job_delivery_id
    assert drop.owner_attempt_id == attempt.attempt_id
    with pytest.raises(DropResourceUnavailableError):
        DropResourceService(session).assert_drop_available_for_material()


def test_fake_operator_empty_return_releases_drop_after_delivery_override(session: Session, monkeypatch):
    job, step = _job(session)
    part = Part(part_code="OVERRIDE-RETURN", part_name="Override return", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part)
    session.flush()
    step.part_code = part.part_code
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="D-RETURN", display_name="D-RETURN",
        status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    session.add(JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id,
        part_code=part.part_code, quantity=1,
    ))
    session.commit()

    override = _service(session)
    monkeypatch.setattr(override, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, delivery.job_delivery_id))
    override.advance(
        job_id=job.job_id,
        expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY,
        expected_entity_id=delivery.job_delivery_id,
    )
    # Consumption completion is pre-existing durable evidence required by the operator return command.
    step.status = StepStatus.COMPLETED
    session.commit()

    fake_transport = FakeForkliftActionTransport()
    coordinator = ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(fake_transport),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    result = OperatorEmptyPalletReturnService(
        session,
        forklift_execution_coordinator=coordinator,
    ).execute_confirmed_empty_return(
        production_job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id,
    )

    assert result.status.value == "SUCCEEDED"
    assert len(fake_transport.execute_transport_requests) == 1
    empty_return = session.query(ExecutionAttempt).filter_by(
        job_delivery_id=delivery.job_delivery_id,
        command_type="EXECUTE_TRANSPORT_EMPTY_RETURN",
    ).one()
    assert empty_return.status is ExecutionAttemptStatus.SUCCEEDED
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE

def test_robot_override_issues_reserved_inventory_and_marks_standard_event(session: Session, monkeypatch):
    job, step = _job(session)
    part = Part(part_code="OVERRIDE-PART", part_name="Override part", category=PartCategory.STRUCTURE, unit="EA")
    inventory = Inventory(part_code=part.part_code, quantity=10, reserved_quantity=2)
    delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code="D-I", display_name="D-I", status=MaterialDeliveryStatus.COMPLETED, supply_group_code="G")
    session.add_all([part, inventory, delivery])
    session.flush()
    step.part_code = part.part_code
    step.quantity = 2
    session.add(JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=part.part_code, quantity=2))
    session.commit()
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id))
    service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id)
    assert (inventory.quantity, inventory.reserved_quantity, inventory.available_quantity) == (8, 0, 8)
    event = session.query(ProductionEvent).filter_by(job_step_id=step.job_step_id, event_type=EventType.STEP_COMPLETED).one()
    assert event.message.startswith("[TEST_OVERRIDE]")

def test_unresolved_forklift_attempt_blocks_delivery_override(session: Session, monkeypatch):
    job, _ = _job(session)
    delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code="D-F", display_name="D-F", status=MaterialDeliveryStatus.PENDING, supply_group_code="G")
    session.add(delivery)
    session.flush()
    session.add(ExecutionAttempt(req_id="forklift-real", executor_type=ExecutorType.FORKLIFT, command_type="EXECUTE_TRANSPORT", job_id=job.job_id, job_delivery_id=delivery.job_delivery_id, attempt_no=1, status=ExecutionAttemptStatus.ACCEPTED, request_payload_json="{}"))
    session.commit()
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, delivery.job_delivery_id))
    with pytest.raises(TestOverrideDeniedError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=delivery.job_delivery_id)
    assert delivery.status is MaterialDeliveryStatus.PENDING


def test_terminal_job_cannot_be_advanced_by_test_override(session: Session):
    job, step = _job(session)
    job.status = JobStatus.CANCELED
    session.commit()

    with pytest.raises(TestOverrideStaleError):
        _service(session).advance(
            job_id=job.job_id,
            expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
            expected_entity_id=step.job_step_id,
        )

def test_pre_roof_vision_wire_inflight_is_never_overridden(session: Session):
    job, _ = _job(session)
    job.status = JobStatus.PRE_ROOF_READY
    inspection = ProductionInspection(production_job_id=job.job_id, inspection_type=ProductionInspectionType.PRE_ROOF, inspection_cycle=1, status=ProductionInspectionStatus.RUNNING, wire_request_snapshot_json="{\"real\":true}")
    session.add(inspection)
    session.commit()
    service = _service(session)
    with pytest.raises(TestOverrideDeniedError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.PRE_ROOF_INSPECTION, expected_entity_id=inspection.inspection_id)


def test_stale_type_or_entity_never_advances_current_blocker(session: Session, monkeypatch):
    job, step = _job(session)
    service = _service(session)
    monkeypatch.setattr(service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id))
    with pytest.raises(TestOverrideStaleError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=step.job_step_id)
    with pytest.raises(TestOverrideStaleError):
        service.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, expected_entity_id=step.job_step_id + 1)
    assert step.status is StepStatus.PENDING


def test_operator_and_manual_blockers_are_not_synthetic(session: Session, monkeypatch):
    job, step = _job(session)
    service = _service(session)
    for blocker in (CurrentBlockerType.OPERATOR_EXECUTION_READY, CurrentBlockerType.MANUAL_PHYSICAL_READY):
        monkeypatch.setattr(service, "_resolve", lambda _job, blocker=blocker: CurrentBlocker(blocker, step.job_step_id))
        with pytest.raises(TestOverrideDeniedError):
            service.advance(job_id=job.job_id, expected_blocker_type=blocker, expected_entity_id=step.job_step_id)
    assert step.status is StepStatus.PENDING


def test_incoming_qa_override_completes_one_mode_per_click_and_notifies(session: Session):
    job = _incoming_qa_job(session)
    qa_events: list[int] = []
    production_events: list[tuple[int, str | None]] = []
    set_incoming_qa_change_callback(qa_events.append)
    set_production_change_callback(lambda job_id, reason: production_events.append((job_id, reason)))
    try:
        service = _service(session)
        first_blocker = service.current_blocker(job_id=job.job_id)
        assert first_blocker is not None
        assert first_blocker.blocker_type is CurrentBlockerType.INCOMING_QA_INSPECTION
        assert first_blocker.entity_id == job.job_id
        first = service.advance(
            job_id=job.job_id,
            expected_blocker_type=first_blocker.blocker_type,
            expected_entity_id=first_blocker.entity_id,
        )
        assert first.advanced.blocker_type is CurrentBlockerType.INCOMING_QA_INSPECTION
        transactions = list(session.query(IncomingQATransaction).filter_by(production_job_id=job.job_id).order_by(IncomingQATransaction.transaction_id))
        assert [(tx.inspection_mode, tx.inspection_cycle, tx.status, tx.overall_result) for tx in transactions] == [
            ("BASE_AB", 1, IncomingQATransactionStatus.COMPLETED, MaterialInspectionResult.PASS)
        ]
        base_items = list(session.query(MaterialInspection).filter_by(incoming_qa_transaction_id=transactions[0].transaction_id))
        assert len(base_items) == 1
        assert all(item.status is MaterialInspectionStatus.COMPLETED and item.result is MaterialInspectionResult.PASS for item in base_items)
        assert transactions[0].production_valid is False

        second_blocker = service.current_blocker(job_id=job.job_id)
        assert second_blocker is not None and second_blocker.blocker_type is CurrentBlockerType.INCOMING_QA_INSPECTION
        service.advance(
            job_id=job.job_id,
            expected_blocker_type=second_blocker.blocker_type,
            expected_entity_id=second_blocker.entity_id,
        )
        transactions = list(session.query(IncomingQATransaction).filter_by(production_job_id=job.job_id).order_by(IncomingQATransaction.transaction_id))
        assert [(tx.inspection_mode, tx.inspection_cycle, tx.status, tx.overall_result) for tx in transactions] == [
            ("BASE_AB", 1, IncomingQATransactionStatus.COMPLETED, MaterialInspectionResult.PASS),
            ("HOUSE_B", 1, IncomingQATransactionStatus.COMPLETED, MaterialInspectionResult.PASS),
        ]
        house_items = list(session.query(MaterialInspection).filter_by(incoming_qa_transaction_id=transactions[1].transaction_id))
        assert len(house_items) == 6
        assert all(item.status is MaterialInspectionStatus.COMPLETED and item.result is MaterialInspectionResult.PASS for item in house_items)
        assert qa_events == [transactions[0].transaction_id, transactions[1].transaction_id]
        assert production_events == [
            (job.job_id, "incoming_qa_test_override_pass"),
            (job.job_id, "incoming_qa_test_override_pass"),
        ]
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id)
        assert readiness.ready is True and readiness.released_items == readiness.total_items == 7
        assert service.current_blocker(job_id=job.job_id) is None
        with pytest.raises(TestOverrideStaleError):
            service.advance(
                job_id=job.job_id,
                expected_blocker_type=CurrentBlockerType.INCOMING_QA_INSPECTION,
                expected_entity_id=job.job_id,
            )
    finally:
        set_incoming_qa_change_callback(None)
        set_production_change_callback(None)


@pytest.mark.parametrize("status", [IncomingQATransactionStatus.REQUESTED, IncomingQATransactionStatus.SENT, IncomingQATransactionStatus.ACKED])
def test_incoming_qa_override_rejects_existing_nonterminal_vision_transaction(session: Session, status):
    job = _incoming_qa_job(session)
    service = _service(session)
    first_blocker = service.current_blocker(job_id=job.job_id)
    assert first_blocker is not None
    if status is IncomingQATransactionStatus.REQUESTED:
        # The initial durable request is intentionally treated as runtime-owned.
        from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
        IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job.job_id)
    else:
        from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
        planned = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job.job_id)
        transaction = session.get(IncomingQATransaction, planned.send_transaction_id)
        assert transaction is not None
        transaction.status = status
        session.commit()
    with pytest.raises(TestOverrideDeniedError):
        service.advance(
            job_id=job.job_id,
            expected_blocker_type=CurrentBlockerType.INCOMING_QA_INSPECTION,
            expected_entity_id=job.job_id,
        )
    assert session.query(IncomingQATransaction).filter_by(production_job_id=job.job_id).count() == 1


def test_incoming_qa_blocker_uses_postgresql_compatible_pending_comparison(session: Session):
    job = _incoming_qa_job(session)
    pending = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_BASE",
        display_name="Base",
        status=StepStatus.PENDING,
    )
    session.add(pending)
    session.commit()

    blocker = _service(session).current_blocker(job_id=job.job_id)
    assert blocker is not None
    assert blocker.blocker_type is CurrentBlockerType.INCOMING_QA_INSPECTION

    # Enum values must compile as ordinary inequality, never PostgreSQL IS NOT.
    statement = select(JobStep.job_step_id).where(
        JobStep.job_id == job.job_id,
        JobStep.status != StepStatus.PENDING,
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert " IS NOT " not in compiled
    assert " != " in compiled

    pending.status = StepStatus.RUNNING
    session.commit()
    assert _service(session).current_blocker(job_id=job.job_id) is None


def test_robot_start_override_holds_running_then_existing_completion_completes_once(session: Session, monkeypatch):
    job, step = _job(session)
    production_events: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: production_events.append((job_id, reason)))
    try:
        service = _service(session)
        monkeypatch.setattr(
            service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id)
        )
        started = service.start_current_robot_cell_blocker(
            job_id=job.job_id,
            expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
            expected_entity_id=step.job_step_id,
        )
        assert started.advanced.entity_id == step.job_step_id
        assert step.status is StepStatus.RUNNING
        assert session.query(ExecutionAttempt).filter_by(job_step_id=step.job_step_id).count() == 0
        assert session.query(ProductionEvent).filter_by(
            job_step_id=step.job_step_id, event_type=EventType.STEP_STARTED
        ).one().message.startswith("[TEST_OVERRIDE] synthetic RUNNING")
        snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=job.job_id)
        assert snapshot.current_step is not None and snapshot.current_step.status is StepStatus.RUNNING
        assert snapshot.next_step is None
        assert production_events == [(job.job_id, "step_started")]

        with pytest.raises(TestOverrideDeniedError):
            service.start_current_robot_cell_blocker(
                job_id=job.job_id,
                expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
                expected_entity_id=step.job_step_id,
            )

        service.advance(
            job_id=job.job_id,
            expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
            expected_entity_id=step.job_step_id,
        )
        assert step.status is StepStatus.COMPLETED
        assert session.query(ProductionEvent).filter_by(
            job_step_id=step.job_step_id, event_type=EventType.STEP_STARTED
        ).count() == 1
        assert session.query(ProductionEvent).filter_by(
            job_step_id=step.job_step_id, event_type=EventType.STEP_COMPLETED
        ).count() == 1
        assert production_events == [(job.job_id, "step_started"), (job.job_id, "step_completed")]
    finally:
        set_production_change_callback(None)


def test_robot_start_override_rejects_active_real_attempt(session: Session, monkeypatch):
    job, step = _job(session)
    session.add(ExecutionAttempt(
        req_id="real-running", executor_type=ExecutorType.ROBOT_CELL, command_type="EXECUTE_TASK",
        job_id=job.job_id, job_step_id=step.job_step_id, attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED, request_payload_json="{}",
    ))
    session.commit()
    service = _service(session)
    monkeypatch.setattr(
        service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id)
    )
    with pytest.raises(TestOverrideDeniedError):
        service.start_current_robot_cell_blocker(
            job_id=job.job_id,
            expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
            expected_entity_id=step.job_step_id,
        )
    assert step.status is StepStatus.PENDING


def test_fms_recovery_leaves_synthetic_running_step_held_without_dispatch(session: Session, monkeypatch):
    job, step = _job(session)
    service = _service(session)
    monkeypatch.setattr(
        service, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id)
    )
    service.start_current_robot_cell_blocker(
        job_id=job.job_id,
        expected_blocker_type=CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION,
        expected_entity_id=step.job_step_id,
    )
    worker = FmsWorker(
        session_factory=lambda: session,
        fms_execution_coordinator_factory=lambda _session: None,
        forklift_execution_coordinator_factory=lambda _session: None,
        material_feed_execution_coordinator_factory=lambda _session: None,
    )
    assert worker._recover_running_robot_step(
        session=session,
        orchestration=ProductionOrchestrationService(session),
        job=job,
        step=step,
    ) is False
    assert step.status is StepStatus.RUNNING
    assert session.query(ExecutionAttempt).filter_by(job_step_id=step.job_step_id).count() == 0


def test_transport_override_and_synthetic_returns_preserve_drop_authority(session: Session, monkeypatch):
    job, outer_step = _job(session)
    part = Part(part_code="OVERRIDE-TRANSPORT", part_name="Override", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part)
    session.flush()
    outer_step.part_code = part.part_code
    inner_step = JobStep(
        job_id=job.job_id, step_order=2, operation_code="INSTALL_TEST", display_name="Inner",
        part_code=part.part_code, status=StepStatus.PENDING,
    )
    outer = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="OUTER", display_name="Outer",
        status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
    )
    inner = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=2, delivery_code="INNER", display_name="Inner",
        status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="INNER_WALL", supply_destination_code="DROP",
    )
    session.add_all([inner_step, outer, inner])
    session.flush()
    session.add_all([
        JobMaterialDeliveryItem(job_delivery_id=outer.job_delivery_id, job_step_id=outer_step.job_step_id, part_code=part.part_code, quantity=1),
        JobMaterialDeliveryItem(job_delivery_id=inner.job_delivery_id, job_step_id=inner_step.job_step_id, part_code=part.part_code, quantity=1),
    ])
    session.commit()

    events: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: events.append((job_id, reason)))
    try:
        override = _service(session)
        monkeypatch.setattr(override, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, outer.job_delivery_id))
        override.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=outer.job_delivery_id)
        forward = session.query(ExecutionAttempt).filter_by(job_delivery_id=outer.job_delivery_id, command_type="EXECUTE_TRANSPORT").one()
        assert forward.status is ExecutionAttemptStatus.SUCCEEDED
        assert DropResourceService(session).get_drop_state().owner_delivery_id == outer.job_delivery_id

        outer_step.status = StepStatus.COMPLETED
        session.commit()
        returned_outer = override.complete_empty_pallet_return_for_test_override(job_id=job.job_id, job_delivery_id=outer.job_delivery_id)
        assert returned_outer.command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN"
        assert returned_outer.status is ExecutionAttemptStatus.SUCCEEDED
        assert outer.status is MaterialDeliveryStatus.COMPLETED
        assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
        with pytest.raises(TestOverrideDeniedError):
            override.complete_empty_pallet_return_for_test_override(job_id=job.job_id, job_delivery_id=outer.job_delivery_id)

        monkeypatch.setattr(override, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, inner.job_delivery_id))
        override.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=inner.job_delivery_id)
        assert DropResourceService(session).get_drop_state().owner_delivery_id == inner.job_delivery_id
        inner_step.status = StepStatus.COMPLETED
        session.commit()
        override.complete_empty_pallet_return_for_test_override(job_id=job.job_id, job_delivery_id=inner.job_delivery_id)
        assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
        assert events[0] == (job.job_id, "material_delivery_completed")
        assert events[-1] == (job.job_id, "empty_pallet_return_completed")
        assert events.count((job.job_id, "material_delivery_completed")) == 2
        assert events.count((job.job_id, "empty_pallet_return_completed")) == 2
    finally:
        set_production_change_callback(None)


def test_manual_delivery_override_never_creates_drop_attempt(session: Session, monkeypatch):
    job, step = _job(session)
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="BASE", display_name="Base",
        status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.MANUAL, supply_group_code="BASE",
    )
    session.add(delivery)
    session.flush()
    session.add(JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code="BASE", quantity=1))
    session.commit()
    override = _service(session)
    monkeypatch.setattr(override, "_resolve", lambda _job: CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, delivery.job_delivery_id))
    override.advance(job_id=job.job_id, expected_blocker_type=CurrentBlockerType.MATERIAL_DELIVERY, expected_entity_id=delivery.job_delivery_id)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    assert session.query(ExecutionAttempt).filter_by(job_delivery_id=delivery.job_delivery_id).count() == 0
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
