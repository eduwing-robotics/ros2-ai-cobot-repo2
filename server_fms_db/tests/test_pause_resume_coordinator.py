from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.cell_status import CellStatusStore
from fms_server.pause_resume_coordinator import PauseResumeCoordinator
from fms_server.robot_cell_control import FakeRobotCellControlPort, RobotCellControlCommand, RobotCellControlResult
from shared.models import Base
from shared.models.factory import (
    EventType,
    ExecutionAttempt,
    ExecutionAttemptControlState,
    ExecutionAttemptStatus,
    ExecutorType,
    JobStatus,
    JobStep,
    Product,
    ProductionEvent,
    ProductionJob,
    ProductionJobControlState,
    StepStatus,
)
from shared.services.production_control_service import (
    ProductionControlOutcome,
    ProductionControlService,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="PAUSE_PRODUCT", product_name="Pause product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _active_robot_cell(session: Session, *, status: JobStatus = JobStatus.RUNNING):
    job = ProductionJob(product_code="PAUSE_PRODUCT", job_code=f"PAUSE-{session.query(ProductionJob).count()}", status=status)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="ASSEMBLE",
        display_name="Assembly",
        status=StepStatus.RUNNING,
    )
    session.add(step)
    session.flush()
    attempt = ExecutionAttempt(
        req_id=f"cell-{job.job_id}-{step.job_step_id}",
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="EXECUTE_TASK",
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED,
        request_payload_json="{}",
    )
    session.add(attempt)
    session.commit()
    return job, step, attempt


def _active_forklift(session: Session):
    job = ProductionJob(product_code="PAUSE_PRODUCT", job_code=f"FORKLIFT-{session.query(ProductionJob).count()}", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    attempt = ExecutionAttempt(
        req_id=f"forklift-{job.job_id}",
        executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT",
        job_id=job.job_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED,
        request_payload_json="{}",
    )
    session.add(attempt)
    session.commit()
    return job, attempt


def _status(store: CellStatusStore, attempt: ExecutionAttempt, state: str, *, req_id: str | None = None, job_id: int | None = None, step_id: int | None = None) -> None:
    store.accept(json.dumps({
        "ver": "0.2", "seq": 1, "ts": "2026-09-08T00:00:00Z", "cell_state": state,
        "active_task": {
            "req_id": req_id if req_id is not None else attempt.req_id,
            "job_id": str(job_id if job_id is not None else attempt.job_id),
            "step_id": str(step_id if step_id is not None else attempt.job_step_id),
            "task_type": "EXECUTE_TASK", "phase": "ASSEMBLY", "current_item": 0,
            "total_items": 1, "progress": 0.5, "robot": "fr5",
        },
        "robots": None, "conveyor": None, "error": None,
    }))


def _events(session: Session, job_id: int, kind: EventType) -> list[ProductionEvent]:
    return list(session.scalars(select(ProductionEvent).where(
        ProductionEvent.job_id == job_id, ProductionEvent.event_type == kind,
    )))


def test_pause_immediate_intent_defaults_false_and_is_cleared_after_held(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    assert job.control_immediate_requested is False

    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id, immediate=True)
    assert job.control_immediate_requested is True

    store = CellStatusStore(); port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    assert coordinator.reconcile_once()
    assert port.request_immediates == [True]
    # ACK remains only an acceptance signal and must retain the request intent
    # for a retry/reconstruction until HELD settles it.
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    assert job.control_immediate_requested is True
    _status(store, attempt, "HELD")
    assert coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.PAUSED
    assert job.control_immediate_requested is False


def test_non_voice_pause_explicitly_overwrites_prior_immediate_intent(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id, immediate=True)
    store = CellStatusStore()
    coordinator = PauseResumeCoordinator(session, control_port=FakeRobotCellControlPort(), cell_status_store=store)
    coordinator.reconcile_once(); _status(store, attempt, "HELD"); coordinator.reconcile_once()
    assert control.request_resume(job_id=job.job_id).outcome is ProductionControlOutcome.RESUME_REQUESTED
    _status(store, attempt, "EXECUTE"); coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.ACTIVE
    assert job.control_immediate_requested is False

    control.request_pause(job_id=job.job_id)
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    assert job.control_immediate_requested is False


def test_restart_reconstructs_durable_immediate_pause_intent(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id, immediate=True)
    # A fresh coordinator has no voice/process-local command context. The DB
    # request remains the sole source of its PAUSE immediate argument.
    port = FakeRobotCellControlPort()
    assert PauseResumeCoordinator(session, control_port=port, cell_status_store=CellStatusStore()).reconcile_once()
    assert port.requests == [(RobotCellControlCommand.PAUSE, job.control_req_id)]
    assert port.request_immediates == [True]
    assert attempt.control_dispatched_at is not None


def test_active_long_running_robot_action_pause_is_dispatched_and_settles_only_after_held(session: Session) -> None:
    job, step, attempt = _active_robot_cell(session)
    service = ProductionControlService(session)
    assert service.request_pause(job_id=job.job_id).outcome is ProductionControlOutcome.PAUSE_REQUESTED
    assert job.status is JobStatus.RUNNING
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED
    assert attempt.control_state is ExecutionAttemptControlState.PAUSE_REQUESTED

    store = CellStatusStore()
    port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    assert coordinator.reconcile_once()
    assert port.requests == [(RobotCellControlCommand.PAUSE, job.control_req_id)]
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED  # ExecuteTask is still alive.
    assert job.status is JobStatus.RUNNING
    assert not _events(session, job.job_id, EventType.JOB_PAUSED)

    assert not coordinator.reconcile_once()  # accepted control ACK alone is not settlement
    _status(store, attempt, "HELD")
    assert coordinator.reconcile_once()
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED
    assert attempt.control_state is ExecutionAttemptControlState.HELD
    assert step.status is StepStatus.RUNNING
    assert job.status is JobStatus.RUNNING
    assert job.control_state is ProductionJobControlState.PAUSED
    assert len(_events(session, job.job_id, EventType.JOB_PAUSED)) == 1
    assert port.requests == [(RobotCellControlCommand.PAUSE, job.control_req_id)]


@pytest.mark.parametrize("wrong", ["req_id", "job_id", "step_id"])
def test_wrong_held_correlation_never_settles_pause(session: Session, wrong: str) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id)
    store = CellStatusStore(); port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    coordinator.reconcile_once()
    kwargs = {wrong: "wrong" if wrong == "req_id" else attempt.job_id + 99}
    _status(store, attempt, "HELD", **kwargs)
    assert not coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    assert job.status is JobStatus.RUNNING
    assert not _events(session, job.job_id, EventType.JOB_PAUSED)


def test_pause_rejection_and_timeout_remain_fail_closed(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id)
    port = FakeRobotCellControlPort(RobotCellControlResult(False, "EXECUTE", "busy"))
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=CellStatusStore())
    assert coordinator.reconcile_once()
    assert attempt.detail == "CELL_CONTROL_PAUSE_REJECTED: busy"
    assert job.status is JobStatus.RUNNING
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED

    attempt.control_dispatched_at = datetime.now(timezone.utc)
    job.control_requested_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.commit()
    assert not coordinator.reconcile_once()
    assert attempt.detail == "CELL_CONTROL_PAUSE_STATUS_TIMEOUT"
    assert job.status is JobStatus.RUNNING


def test_pause_resume_reuses_same_attempt_and_requires_correlated_execute(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session, status=JobStatus.PRE_ROOF_READY)
    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id)
    store = CellStatusStore(); port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    coordinator.reconcile_once(); _status(store, attempt, "HELD"); coordinator.reconcile_once()
    original_attempt_id = attempt.attempt_id

    assert control.request_resume(job_id=job.job_id).outcome is ProductionControlOutcome.RESUME_REQUESTED
    assert job.control_state is ProductionJobControlState.RESUME_REQUESTED
    assert attempt.attempt_id == original_attempt_id
    assert attempt.control_state is ExecutionAttemptControlState.RESUME_REQUESTED
    assert coordinator.reconcile_once()
    assert port.requests[-1] == (RobotCellControlCommand.RESUME, job.control_req_id)
    assert job.status is JobStatus.PRE_ROOF_READY
    assert not _events(session, job.job_id, EventType.JOB_RESUMED)
    _status(store, attempt, "EXECUTE")
    assert coordinator.reconcile_once()
    assert job.status is JobStatus.PRE_ROOF_READY
    assert job.control_state is ProductionJobControlState.ACTIVE
    assert attempt.control_state is ExecutionAttemptControlState.ACTIVE
    assert len(_events(session, job.job_id, EventType.JOB_RESUMED)) == 1
    assert not coordinator.reconcile_once()
    assert len(port.requests) == 2


def test_no_executor_settles_without_control_call_and_forklift_is_unsupported(session: Session) -> None:
    job = ProductionJob(product_code="PAUSE_PRODUCT", job_code="NO-EXECUTOR", status=JobStatus.READY)
    session.add(job); session.commit()
    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id)
    port = FakeRobotCellControlPort()
    assert PauseResumeCoordinator(session, control_port=port, cell_status_store=None).reconcile_once()
    assert job.status is JobStatus.READY and job.control_state is ProductionJobControlState.PAUSED
    assert port.requests == []

    forklift_job, forklift_attempt = _active_forklift(session)
    result = control.request_pause(job_id=forklift_job.job_id)
    assert result.outcome is ProductionControlOutcome.ACTIVE_FORKLIFT_PHYSICAL_PAUSE_NOT_SUPPORTED
    assert forklift_job.status is JobStatus.RUNNING
    assert forklift_job.control_state is ProductionJobControlState.ACTIVE
    assert forklift_attempt.status is ExecutionAttemptStatus.ACCEPTED


def test_restart_reconciles_existing_held_without_redispatch(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id)
    marker = datetime.now(timezone.utc)
    attempt.control_dispatched_at = marker
    session.commit()
    store = CellStatusStore(); _status(store, attempt, "HELD")
    new_port = FakeRobotCellControlPort()
    fresh_coordinator = PauseResumeCoordinator(session, control_port=new_port, cell_status_store=store)
    assert fresh_coordinator.reconcile_once()
    assert job.status is JobStatus.RUNNING
    assert new_port.requests == []
    assert len(_events(session, job.job_id, EventType.JOB_PAUSED)) == 1



def _status_v03(
    store: CellStatusStore,
    attempt: ExecutionAttempt,
    state: str,
    *,
    pause_req_id: str,
    resumable: bool = True,
    held_at: str = "PHASE_BOUNDARY",
    hold_task_req_id: str | None = None,
    error: str | None = None,
) -> None:
    store.accept(json.dumps({
        "ver": "0.3", "seq": 99, "ts": "2026-09-10T00:00:00Z", "cell_state": state,
        "active_task": {
            "req_id": attempt.req_id, "job_id": str(attempt.job_id), "step_id": str(attempt.job_step_id),
            "task_type": "EXECUTE_TASK", "phase": "ASSEMBLY", "current_item": 0,
            "total_items": 1, "progress": 0.5, "robot": "fr5",
        },
        "hold": {
            "task_req_id": hold_task_req_id if hold_task_req_id is not None else attempt.req_id, "pause_req_id": pause_req_id,
            "stop_mode": "DEFERRED_UNSAFE", "held_at": held_at, "phase": "ASSEMBLY",
            "since": "2026-09-10T00:00:00Z", "resumable": resumable,
        },
        "robots": None, "conveyor": None, "error": error,
    }))


def test_v03_held_requires_matching_pause_request_and_preserves_actual_held_at(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id)
    store = CellStatusStore()
    port = FakeRobotCellControlPort(RobotCellControlResult(True, "EXECUTE", "ack", "DEFERRED_UNSAFE", 2500))
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    assert coordinator.reconcile_once()
    assert port.request_immediates == [False]
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    _status_v03(store, attempt, "HELD", pause_req_id="stale-pause", held_at="HOVER")
    assert not coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    assert store.latest().hold["held_at"] == "HOVER"
    assert store.latest().hold["stop_mode"] == "DEFERRED_UNSAFE"
    _status_v03(store, attempt, "HELD", pause_req_id=job.control_req_id or "")
    assert coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.PAUSED


def test_v03_non_resumable_held_blocks_resume_without_execute_task_redispatch(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id)
    store = CellStatusStore(); port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    coordinator.reconcile_once()
    pause_req_id = job.control_req_id or ""
    _status_v03(store, attempt, "HELD", pause_req_id=pause_req_id, resumable=False)
    assert coordinator.reconcile_once()
    assert control.request_resume(job_id=job.job_id).outcome is ProductionControlOutcome.RESUME_REQUESTED
    assert not coordinator.reconcile_once()
    assert port.requests == [(RobotCellControlCommand.PAUSE, pause_req_id)]
    assert attempt.detail == "CELL_CONTROL_RESUME_NOT_RESUMABLE_RESET_REQUIRED"
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED


def test_resume_unverified_status_never_settles_as_execute(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    control = ProductionControlService(session)
    control.request_pause(job_id=job.job_id)
    store = CellStatusStore(); port = FakeRobotCellControlPort()
    coordinator = PauseResumeCoordinator(session, control_port=port, cell_status_store=store)
    coordinator.reconcile_once(); pause_req_id = job.control_req_id or ""
    _status_v03(store, attempt, "HELD", pause_req_id=pause_req_id)
    assert coordinator.reconcile_once()
    control.request_resume(job_id=job.job_id)
    assert coordinator.reconcile_once()  # RESUME ACK request only
    _status_v03(store, attempt, "EXECUTE", pause_req_id=pause_req_id, error="RESUME_UNVERIFIED")
    assert not coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.RESUME_REQUESTED
    assert attempt.detail.startswith("CELL_CONTROL_RESUME_UNVERIFIED")



def test_v03_held_with_wrong_hold_task_request_never_settles_pause(session: Session) -> None:
    job, _, attempt = _active_robot_cell(session)
    ProductionControlService(session).request_pause(job_id=job.job_id)
    store = CellStatusStore(); coordinator = PauseResumeCoordinator(
        session, control_port=FakeRobotCellControlPort(), cell_status_store=store
    )
    coordinator.reconcile_once()
    _status_v03(
        store, attempt, "HELD", pause_req_id=job.control_req_id or "",
        hold_task_req_id="other-execute-task",
    )
    assert not coordinator.reconcile_once()
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
