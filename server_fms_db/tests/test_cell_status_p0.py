from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.cell_status import CellStatusStore, CellStatusValidationError
from fms_server.execution_coordinator import CoordinatorOutcome, FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from shared.models import Base
from shared.models.factory import ExecutionAttempt, ExecutionAttemptStatus, JobStep, Product, RoofOptionCode, StepStatus
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from tests.recipe_test_support import HOUSE_A_STAGES, add_active_recipe, seed_complete_incoming_qa


def status_payload(*, state: str = "IDLE", seq: int = 1, active_task=None) -> str:
    return json.dumps({"ver": "0.2", "seq": seq, "ts": "2026-08-25T05:10:14.843Z", "cell_state": state, "active_task": active_task, "robots": {"fr5": {"state": "IDLE", "connected": True, "joints": [], "error": None}}, "conveyor": {"running": False}, "error": None})


def test_cs1_valid_status_v02_is_accepted() -> None:
    now = [10.0]; store = CellStatusStore(monotonic_clock=lambda: now[0])
    snapshot = store.accept(status_payload())
    assert snapshot.seq == 1 and snapshot.cell_state == "IDLE"


@pytest.mark.parametrize("bad", ["{", "[]", json.dumps({"ver": "9.9", "seq": 1, "ts": "x", "cell_state": "IDLE", "active_task": None}), json.dumps({"ver": "0.2", "seq": 1, "ts": "x", "cell_state": {"bad": "state"}, "active_task": None})])
def test_cs2_cs3_invalid_or_wrong_version_is_ignored(bad: str) -> None:
    store = CellStatusStore(monotonic_clock=lambda: 0.0)
    with pytest.raises(CellStatusValidationError):
        store.accept(bad)
    assert store.latest() is None and not store.heartbeat_available()


def test_cs4_to_cs7_fresh_stale_and_recovery_without_sleep() -> None:
    now = [0.0]; store = CellStatusStore(monotonic_clock=lambda: now[0])
    assert not store.heartbeat_available()  # CS5
    store.accept(status_payload())
    assert store.heartbeat_available() and store.dispatch_availability().dispatch_allowed  # CS4/CS8
    now[0] = 3.01
    assert not store.heartbeat_available() and store.dispatch_availability().reason == "CELL_STATUS_STALE"  # CS6
    store.accept(status_payload(seq=2))
    assert store.heartbeat_available() and store.dispatch_availability().dispatch_allowed  # CS7


@pytest.mark.parametrize("state", ["EXECUTE", "HELD", "ABORTED", "FAULT"])
def test_cs9_to_cs12_fresh_non_idle_states_block_new_dispatch(state: str) -> None:
    store = CellStatusStore(monotonic_clock=lambda: 1.0); store.accept(status_payload(state=state))
    decision = store.dispatch_availability()
    assert decision.heartbeat_available and not decision.dispatch_allowed
    assert decision.reason == f"CELL_STATE_{state}"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_A", product_name="A형 주택")); db.flush()
    add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES); db.commit()
    try:
        yield db
    finally:
        db.close(); Base.metadata.drop_all(engine); engine.dispose()


def _coordinator(session: Session, store: CellStatusStore, result: CellTaskExecutionResult):
    transport = FakeCellActionTransport([FakeCellActionExchange(result)])
    return FmsExecutionCoordinator(session, orchestration_service=ProductionOrchestrationService(session), step_readiness_service=StepReadinessService(MaterialDeliveryService(session)), robot_cell_adapter=RobotCellActionAdapter(transport), execution_attempt_service=ExecutionAttemptService(session), cell_status_store=store), transport


def _job_step(session: Session):
    service = ProductionOrchestrationService(session)
    job = service.create_job(product_code="HOUSE_A", job_code=f"CELL-STATUS-{uuid.uuid4().hex}", roof_option_code=RoofOptionCode.ROOF_01)
    seed_complete_incoming_qa(session, job_id=job.job_id)
    service.start_job(job.job_id)
    step = service.get_next_step(job.job_id)
    assert step is not None
    return job, step


def _execute(session: Session, coordinator: FmsExecutionCoordinator, job_id: int, step_id: int):
    return coordinator.execute_step(job_id=job_id, job_step_id=step_id, req_id=f"cell-status-{uuid.uuid4()}", parts_json='[{"slot":"S","class":"base_house_a"}]')


def test_cs13_unavailable_sends_zero_goals_and_creates_no_attempt(session: Session) -> None:
    store = CellStatusStore(monotonic_clock=lambda: 0.0)
    coordinator, transport = _coordinator(session, store, CellTaskExecutionResult.success())
    job, step = _job_step(session); result = _execute(session, coordinator, job.job_id, step.job_step_id)
    assert result.outcome is CoordinatorOutcome.CELL_UNAVAILABLE and result.detail == "CELL_STATUS_NOT_RECEIVED"
    assert transport.commands == []
    assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
    assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING


def test_cs14_cs15_stale_after_dispatch_does_not_change_unknown_attempt(session: Session) -> None:
    now = [0.0]; store = CellStatusStore(monotonic_clock=lambda: now[0]); store.accept(status_payload())
    coordinator, transport = _coordinator(session, store, CellTaskExecutionResult.result_timeout(detail="waiting"))
    job, step = _job_step(session); result = _execute(session, coordinator, job.job_id, step.job_step_id)
    assert result.outcome is CoordinatorOutcome.UNCERTAIN_RESULT_TIMEOUT
    now[0] = 4.0
    assert store.dispatch_availability().reason == "CELL_STATUS_STALE"
    assert session.get(JobStep, step.job_step_id).status is StepStatus.RUNNING
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.job_step_id == step.job_step_id))
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.UNKNOWN
    assert len(transport.commands) == 1


def test_cs16_action_result_remains_only_completion_source(session: Session) -> None:
    store = CellStatusStore(monotonic_clock=lambda: 0.0); store.accept(status_payload())
    coordinator, _ = _coordinator(session, store, CellTaskExecutionResult.success())
    job, step = _job_step(session); result = _execute(session, coordinator, job.job_id, step.job_step_id)
    assert result.outcome is CoordinatorOutcome.COMPLETED
    assert session.get(JobStep, step.job_step_id).status is StepStatus.COMPLETED
