import asyncio
import threading

import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from shared.models.factory import (
    Base, ProductionJob, JobStep, JobStatus, StepStatus,
    Product, ExecutionAttempt, ExecutionAttemptStatus, ExecutorType, JobMaterialDelivery, JobMaterialDeliveryItem, Inventory,
    JobMaterialFeedExecution, MaterialDeliveryStatus, MaterialFeedStatus, Part, PartCategory,
    ProductionJobControlState
)
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.config import MaterialPrefetchMode

from fms_server.main import reconcile_pause_resume_once
from fms_server.worker import FmsWorker
from fms_server.execution_coordinator import FmsExecutionCoordinator, CoordinatorOutcome
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, CellTaskExecutionResult
from fms_server.fake_cell_action_transport import FakeCellActionTransport, FakeCellActionExchange
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.forklift_action_adapter import ForkliftActionAdapter
from fms_server.forklift_action_adapter import FakeForkliftActionTransport
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from tests.recipe_test_support import seed_complete_incoming_qa


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as sess:
        yield sess


def setup_worker(session_factory):
    cell_transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(1000)])
    forklift_transport = FakeForkliftActionTransport()

    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(forklift_transport)

    def fms_execution_coordinator_factory(session):
        return FmsExecutionCoordinator(
            session,
            orchestration_service=ProductionOrchestrationService(session),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(session)
        )

    def forklift_execution_coordinator_factory(session):
        return ForkliftExecutionCoordinator(
            session,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(session)
        )

    def material_feed_execution_coordinator_factory(session):
        return MaterialFeedExecutionCoordinator(
            session,
            robot_cell_adapter=cell_adapter,
        )

    worker = FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_execution_coordinator_factory,
        forklift_execution_coordinator_factory=forklift_execution_coordinator_factory,
        material_feed_execution_coordinator_factory=material_feed_execution_coordinator_factory
    )
    return worker, cell_transport, forklift_transport


def _seed_ready_legacy_materials(session, job, steps):
    """Give worker-only fixtures an explicit legacy delivery, feed, and global QA release."""
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"{job.job_code}-DEL",
        display_name="Worker fixture delivery", status=MaterialDeliveryStatus.COMPLETED,
    )
    session.add(delivery)
    session.flush()
    session.add(JobMaterialFeedExecution(
        job_delivery_id=delivery.job_delivery_id, status=MaterialFeedStatus.COMPLETED,
    ))
    for step in steps:
        if session.get(Part, step.part_code) is None:
            session.add(Part(part_code=step.part_code, part_name=step.part_code,
                vision_class=step.vision_class or "wall_ext", category=PartCategory.STRUCTURE, unit="EA"))
        inventory = session.get(Inventory, step.part_code)
        if inventory is None:
            inventory = Inventory(part_code=step.part_code, quantity=100, reserved_quantity=0)
            session.add(inventory)
        inventory.reserved_quantity += 1
        session.add(JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id,
            job_step_id=step.job_step_id, part_code=step.part_code, quantity=1))
    steps[-1].is_terminal = True
    session.flush()
    seed_complete_incoming_qa(session, job_id=job.job_id)


def test_L1_empty_db(session_factory):
    worker, _, _ = setup_worker(session_factory)
    dispatched = worker.tick()
    assert not dispatched


def test_L2_no_dispatchable_step(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.COMPLETED)
    session.add_all([p, job])
    session.commit()

    worker, _, _ = setup_worker(session_factory)
    dispatched = worker.tick()
    assert not dispatched


def test_L3_one_dispatchable_step(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    step = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    session.add_all([p, job, step])
    session.commit()
    _seed_ready_legacy_materials(session, job, [step])

    worker, cell_transport, _ = setup_worker(session_factory)
    dispatched = worker.tick()
    assert dispatched

    # Check outcome
    assert len(cell_transport.commands) == 1
    session.refresh(step)
    assert step.status == StepStatus.COMPLETED


def test_L4_duplicate_iteration(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    step = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    session.add_all([p, job, step])
    session.commit()
    _seed_ready_legacy_materials(session, job, [step])

    worker, cell_transport, _ = setup_worker(session_factory)

    # First tick executes it
    assert worker.tick()

    # Second tick does nothing
    assert not worker.tick()
    assert len(cell_transport.commands) == 1


@pytest.mark.parametrize(
    ("control_state", "prefetch_mode"),
    [
        (ProductionJobControlState.PAUSE_REQUESTED, MaterialPrefetchMode.DISABLED),
        (ProductionJobControlState.PAUSED, MaterialPrefetchMode.DISABLED),
        (ProductionJobControlState.RESUME_REQUESTED, MaterialPrefetchMode.ONE_AHEAD),
    ],
)
def test_pause_control_state_suppresses_normal_and_one_ahead_dispatch(
    session_factory, session, control_state, prefetch_mode,
):
    """Transient control states block physical work before it reaches Cell/Forklift."""
    product = Product(product_code="PAUSE_GUARD", product_name="Pause guard")
    job = ProductionJob(
        product_code=product.product_code,
        job_code=f"PAUSE-GUARD-{control_state.value}-{prefetch_mode.value}",
        status=JobStatus.RUNNING,
        control_state=control_state,
    )
    session.add_all([product, job])
    session.flush()
    step = JobStep(
        job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL",
        status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left",
        slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code=f"PAUSE-PART-{job.job_id}",
    )
    session.add(step)
    session.commit()
    _seed_ready_legacy_materials(session, job, [step])

    worker, cell_transport, forklift_transport = setup_worker(session_factory)
    worker.set_material_prefetch_mode(prefetch_mode)
    assert not worker.tick()
    session.refresh(step)
    assert step.status is StepStatus.PENDING
    assert cell_transport.commands == []
    assert forklift_transport.execute_transport_requests == []


def test_L5_multiple_sequential_steps(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    s2 = JobStep(job_id=1, step_order=2, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT2", pick_zone="CONVEYOR_PICK", part_code="W2")
    s3 = JobStep(job_id=1, step_order=3, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT3", pick_zone="CONVEYOR_PICK", part_code="W3")
    session.add_all([p, job, s1, s2, s3])
    session.commit()
    _seed_ready_legacy_materials(session, job, [s1, s2, s3])

    worker, cell_transport, _ = setup_worker(session_factory)

    assert worker.tick()
    session.refresh(s1)
    assert s1.status == StepStatus.COMPLETED
    assert s2.status == StepStatus.PENDING

    assert worker.tick()
    session.refresh(s2)
    assert s2.status == StepStatus.COMPLETED

    assert worker.tick()
    session.refresh(s3)
    assert s3.status == StepStatus.COMPLETED

    assert not worker.tick()

    assert len(cell_transport.commands) == 3


def test_L6_fake_failure(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    step = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    session.add_all([p, job, step])
    session.commit()
    _seed_ready_legacy_materials(session, job, [step])

    worker, cell_transport, _ = setup_worker(session_factory)
    # Mock failure
    cell_transport._exchanges = [FakeCellActionExchange(CellTaskExecutionResult.cell_failed(error_code="error", detail="fake error"))]

    assert worker.tick()

    session.refresh(step)
    assert step.status == StepStatus.FAILED

    # Tick again should not retry
    assert not worker.tick()


def test_L7_unknown(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    step = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    session.add_all([p, job, step])
    session.commit()
    _seed_ready_legacy_materials(session, job, [step])

    worker, cell_transport, _ = setup_worker(session_factory)
    cell_transport._exchanges = [FakeCellActionExchange(CellTaskExecutionResult.transport_error(detail="timeout"))]

    assert worker.tick()

    session.refresh(step)
    assert step.status == StepStatus.PENDING  # It stays PENDING because FmsExecutionCoordinator returns UNCERTAIN

    # Check attempt is UNKNOWN
    attempt = session.query(ExecutionAttempt).first()
    assert attempt.status == ExecutionAttemptStatus.UNKNOWN

    # Tick again should not retry because attempt is not terminal and step isn't re-dispatchable safely unless told.
    # Actually, the worker DOES retry if it's PENDING and there is no RUNNING attempt. Wait.
    # FmsExecutionCoordinator does not block re-dispatch if step is PENDING!
    # Let's fix this in worker: if the job has a PENDING step but there is an active/UNKNOWN attempt, we should skip it.
    # We will test this behavior below.

def _add_running_robot_attempt(
    session: Session,
    *,
    job: ProductionJob,
    step: JobStep,
    status: ExecutionAttemptStatus,
    detail: str | None = None,
    error_code: str | None = None,
) -> ExecutionAttempt:
    attempt = ExecutionAttempt(
        req_id=f"restart-{step.job_step_id}-{status.value}",
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="INSTALL_WALL",
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        attempt_no=1,
        status=status,
        request_payload_json="{}",
        detail=detail,
        error_code=error_code,
    )
    session.add(attempt)
    session.commit()
    return attempt


def test_restart_after_completed_base_dispatches_only_pending_inner(session_factory, session):
    product = Product(product_code="RESTART_A", product_name="Restart A")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RESTART-COMPLETED-BASE", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    base = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.COMPLETED,
                   display_name="Base already complete", vision_class="wall_ext_back", slot_code="BASE", pick_zone="CONVEYOR_PICK", part_code="RESTART-BASE")
    inner = JobStep(job_id=job.job_id, step_order=2, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING,
                    display_name="Inner pending", vision_class="wall_ext_left", slot_code="INNER", pick_zone="CONVEYOR_PICK", part_code="RESTART-INNER")
    session.add_all([base, inner])
    session.commit()
    _seed_ready_legacy_materials(session, job, [base, inner])

    restarted_worker, cell_transport, _ = setup_worker(session_factory)
    assert restarted_worker.tick()

    session.refresh(base)
    session.refresh(inner)
    assert base.status is StepStatus.COMPLETED
    assert inner.status is StepStatus.COMPLETED
    assert len(cell_transport.commands) == 1
    assert cell_transport.commands[0].step_id == str(inner.job_step_id)


def test_restart_running_nonterminal_attempt_fails_closed_without_cell_replay(session_factory, session):
    product = Product(product_code="RESTART_B", product_name="Restart B")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RESTART-UNCERTAIN", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.RUNNING,
                   display_name="Uncertain", vision_class="wall_ext_left", slot_code="UNCERTAIN", pick_zone="CONVEYOR_PICK", part_code="RESTART-UNCERTAIN-PART", is_terminal=True)
    session.add(step)
    session.commit()
    attempt = _add_running_robot_attempt(session, job=job, step=step, status=ExecutionAttemptStatus.ACCEPTED)

    restarted_worker, cell_transport, _ = setup_worker(session_factory)
    assert restarted_worker.tick()

    session.refresh(step)
    session.refresh(job)
    session.refresh(attempt)
    assert step.status is StepStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert step.failure_reason is not None and "manual recovery" in step.failure_reason
    assert attempt.status is ExecutionAttemptStatus.UNKNOWN
    assert len(cell_transport.commands) == 0
    assert not restarted_worker.tick()
    assert len(cell_transport.commands) == 0


def test_restart_running_step_without_attempt_fails_closed_instead_of_skipping_forever(session_factory, session):
    product = Product(product_code="RESTART_NO_ATTEMPT", product_name="Restart missing attempt")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RESTART-NO-ATTEMPT", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.RUNNING,
                   display_name="Missing attempt", vision_class="wall_ext_left", slot_code="NO-ATTEMPT", pick_zone="CONVEYOR_PICK", part_code="RESTART-NO-ATTEMPT-PART", is_terminal=True)
    session.add(step)
    session.commit()

    restarted_worker, cell_transport, _ = setup_worker(session_factory)
    assert restarted_worker.tick()

    session.refresh(step)
    session.refresh(job)
    assert step.status is StepStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert step.failure_reason is not None and "without a Robot Cell execution attempt" in step.failure_reason
    assert len(cell_transport.commands) == 0


def test_restart_reconciles_succeeded_attempt_without_cell_replay(session_factory, session):
    product = Product(product_code="RESTART_C", product_name="Restart C")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RESTART-SUCCEEDED", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.RUNNING,
                   display_name="Succeeded but uncommitted", vision_class="wall_ext_left", slot_code="SUCCEEDED", pick_zone="CONVEYOR_PICK", part_code="RESTART-SUCCEEDED-PART", is_terminal=True)
    session.add(step)
    session.commit()
    attempt = _add_running_robot_attempt(session, job=job, step=step, status=ExecutionAttemptStatus.SUCCEEDED)

    restarted_worker, cell_transport, _ = setup_worker(session_factory)
    assert restarted_worker.tick()

    session.refresh(step)
    session.refresh(job)
    session.refresh(attempt)
    assert step.status is StepStatus.COMPLETED
    assert job.status is JobStatus.COMPLETED
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert len(cell_transport.commands) == 0


def test_restart_reconciles_failed_attempt_without_cell_replay(session_factory, session):
    product = Product(product_code="RESTART_D", product_name="Restart D")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RESTART-FAILED", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.RUNNING,
                   display_name="Failed but uncommitted", vision_class="wall_ext_left", slot_code="FAILED", pick_zone="CONVEYOR_PICK", part_code="RESTART-FAILED-PART", is_terminal=True)
    session.add(step)
    session.commit()
    attempt = _add_running_robot_attempt(
        session, job=job, step=step, status=ExecutionAttemptStatus.FAILED,
        detail="Persisted Cell failure", error_code="CELL_FAKE_FAILED",
    )

    restarted_worker, cell_transport, _ = setup_worker(session_factory)
    assert restarted_worker.tick()

    session.refresh(step)
    session.refresh(job)
    session.refresh(attempt)
    assert step.status is StepStatus.FAILED
    assert job.status is JobStatus.FAILED
    assert step.failure_reason == "Persisted Cell failure"
    assert attempt.status is ExecutionAttemptStatus.FAILED
    assert len(cell_transport.commands) == 0


def test_L8_pre_roof_gate(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.PRE_ROOF_READY)
    session.add_all([p, job])
    session.commit()
    worker, cell_transport, _ = setup_worker(session_factory)
    assert not worker.tick()
    assert len(cell_transport.commands) == 0


def test_P0_four_wall_fake_e2e(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_back", slot_code="HOUSE_A_OUTER_WALL_REAR_01", pick_zone="CONVEYOR_PICK", part_code="WALL-EXT-BACK-BLK01")
    s2 = JobStep(job_id=1, step_order=2, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_door", slot_code="HOUSE_A_OUTER_WALL_DOOR_01", pick_zone="CONVEYOR_PICK", part_code="WALL-EXT-DOOR-BLK01")
    s3 = JobStep(job_id=1, step_order=3, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="HOUSE_A_OUTER_WALL_LEFT_01", pick_zone="CONVEYOR_PICK", part_code="WALL-EXT-LEFT-BLK01")
    s4 = JobStep(job_id=1, step_order=4, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_right", slot_code="HOUSE_A_OUTER_WALL_RIGHT_01", pick_zone="CONVEYOR_PICK", part_code="WALL-EXT-RIGHT-BLK01")
    session.add_all([p, job, s1, s2, s3, s4])
    session.commit()
    _seed_ready_legacy_materials(session, job, [s1, s2, s3, s4])

    worker, cell_transport, _ = setup_worker(session_factory)

    assert worker.tick()
    assert worker.tick()
    assert worker.tick()
    assert worker.tick()

    session.refresh(s1)
    session.refresh(s4)
    assert s1.status == StepStatus.COMPLETED
    assert s4.status == StepStatus.COMPLETED

    assert len(cell_transport.commands) == 4

    verify_commands(cell_transport.commands)
    session.refresh(job)
    assert job.status == JobStatus.COMPLETED

def test_L9_failed_job_is_skipped(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.FAILED)
    step = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    session.add_all([p, job, step])
    session.commit()
    worker, cell_transport, _ = setup_worker(session_factory)
    assert not worker.tick()
    assert len(cell_transport.commands) == 0


def test_L10_multiple_active_jobs_interleaved(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job1 = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    job2 = ProductionJob(product_code="HOUSE_A", job_code="J2", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1")
    s2 = JobStep(job_id=2, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step2", vision_class="wall_ext_left", slot_code="SLOT2", pick_zone="CONVEYOR_PICK", part_code="W2")
    session.add_all([p, job1, job2, s1, s2])
    session.commit()
    _seed_ready_legacy_materials(session, job1, [s1])
    _seed_ready_legacy_materials(session, job2, [s2])

    worker, cell_transport, _ = setup_worker(session_factory)

    # Tick 1 should dispatch for job1 (and potentially job2 in same tick, since we process all)
    assert worker.tick()

    session.refresh(s1)
    session.refresh(s2)

    # Both should be completed because worker loop iterates all active jobs
    assert s1.status == StepStatus.COMPLETED
    assert s2.status == StepStatus.COMPLETED
    assert len(cell_transport.commands) == 2


def verify_commands(commands):
    import json
    for cmd in commands:
        parts_json = cmd.parts_json
        assert parts_json != "[]"
        parsed = json.loads(parts_json)
        assert isinstance(parsed, list)
        assert len(parsed) >= 1
        part = parsed[0]
        assert "part_code" in part
        assert "class" in part
        assert "slot" in part
        assert "zone" in part
        assert "vision_class" not in part
        assert "slot_code" not in part
        assert "pick_zone" not in part
        assert part["zone"] == "CONVEYOR_PICK"

def test_P0_parts_json_verification(session_factory, session):
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="WALL-EXT-LEFT-BLK01")
    session.add_all([p, job, s1])
    session.commit()
    _seed_ready_legacy_materials(session, job, [s1])

    worker, cell_transport, _ = setup_worker(session_factory)
    assert worker.tick()

    verify_commands(cell_transport.commands)


def test_pause_resume_reconciliation_creates_and_closes_session_inside_to_thread() -> None:
    main_thread = threading.get_ident()
    created_in: list[int] = []
    used_in: list[int] = []
    closed_in: list[int] = []

    class ProbeSession:
        def __enter__(self):
            used_in.append(threading.get_ident())
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            closed_in.append(threading.get_ident())

    def probe_session_factory() -> ProbeSession:
        created_in.append(threading.get_ident())
        return ProbeSession()

    class ProbeCoordinator:
        def __init__(self, session: ProbeSession) -> None:
            assert isinstance(session, ProbeSession)

        def reconcile_once(self) -> bool:
            used_in.append(threading.get_ident())
            return True

    async def run() -> bool:
        return await asyncio.to_thread(
            reconcile_pause_resume_once,
            probe_session_factory,
            ProbeCoordinator,
        )

    assert asyncio.run(run()) is True
    assert created_in == used_in[:1]
    assert closed_in == created_in
    assert created_in[0] != main_thread
