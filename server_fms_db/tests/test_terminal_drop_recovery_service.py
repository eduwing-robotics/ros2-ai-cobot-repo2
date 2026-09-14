import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from scripts.recover_terminal_drop import _require_benchmark_identity
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    SupplyMode,
    JobStep,
    StepStatus,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    DropResourceService,
    DropResourceState,
)
from shared.realtime.production_events import set_production_change_callback
from shared.services.test_override_service import TestOverrideService
from shared.services.test_override_transport_service import (
    TestOverrideTransportError as _TestOverrideTransportError,
    TestOverrideTransportService as _TestOverrideTransportService,
)
from shared.services.terminal_drop_recovery_service import (
    TerminalDropAlreadyRecovered,
    TerminalDropRecoveryError,
    TerminalDropRecoveryService,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="RECOVERY_PRODUCT", product_name="Recovery product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def _target(
    session: Session,
    *,
    job_status: JobStatus = JobStatus.COMPLETED,
    group: str = "INNER_WALL",
) -> tuple[ProductionJob, JobMaterialDelivery, ExecutionAttempt]:
    job = ProductionJob(job_code=f"RECOVERY-{job_status.value}-{group}", product_code="RECOVERY_PRODUCT", status=job_status)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=f"RECOVERY-DEL-{job.job_id}",
        display_name="Recovery delivery",
        status=MaterialDeliveryStatus.COMPLETED,
        supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code=group,
        supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    forward = ExecutionAttempt(
        req_id=f"forward-{delivery.job_delivery_id}",
        executor_type=ExecutorType.FORKLIFT,
        command_type=MATERIAL_TRANSPORT_COMMAND_TYPE,
        job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({
            "job_id": job.job_id,
            "delivery_id": delivery.job_delivery_id,
            "pickup_code": "RACK1",
            "dropoff_code": "DROP",
        }),
    )
    session.add(forward)
    session.commit()
    return job, delivery, forward


def _returns(session: Session, delivery_id: int) -> list[ExecutionAttempt]:
    return list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.job_delivery_id == delivery_id,
        ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
    )))


def test_terminal_delivery_recovery_creates_canonical_success_and_releases_drop(session: Session) -> None:
    job, delivery, forward = _target(session)
    before = (job.status, delivery.status, forward.status, forward.request_payload_json)
    assessment = TerminalDropRecoveryService(session).assess(
        job_id=job.job_id, delivery_id=delivery.job_delivery_id
    )
    assert assessment.forward_attempt_id == forward.attempt_id
    assert assessment.drop_state is DropResourceState.OCCUPIED
    assert assessment.drop_owner_delivery_id == delivery.job_delivery_id
    assert (assessment.pickup_code, assessment.dropoff_code) == ("DROP", "RACK2")

    result = TerminalDropRecoveryService(session).recover(
        job_id=job.job_id, delivery_id=delivery.job_delivery_id
    )

    session.expire_all()
    returned = session.get(ExecutionAttempt, result.attempt_id)
    assert returned is not None
    assert returned.command_type == EMPTY_RETURN_COMMAND_TYPE
    assert returned.executor_type is ExecutorType.FORKLIFT
    assert returned.status is ExecutionAttemptStatus.SUCCEEDED
    assert returned.job_id == job.job_id
    assert returned.job_delivery_id == delivery.job_delivery_id
    assert returned.dispatch_started_at is not None and returned.completed_at is not None
    assert json.loads(returned.request_payload_json) == {
        "req_id": result.req_id,
        "job_id": job.job_id,
        "delivery_id": delivery.job_delivery_id,
        "pickup_code": "DROP",
        "dropoff_code": "RACK2",
    }
    assert json.loads(returned.result_payload_json) == {
        "recovery": "BENCHMARK_TERMINAL_DROP_RECOVERY", "status": "SUCCEEDED"
    }
    assert "BENCHMARK_TERMINAL_DROP_RECOVERY" in (returned.detail or "")
    assert (session.get(ProductionJob, job.job_id).status, session.get(JobMaterialDelivery, delivery.job_delivery_id).status) == before[:2]
    unchanged_forward = session.get(ExecutionAttempt, forward.attempt_id)
    assert (unchanged_forward.status, unchanged_forward.request_payload_json) == before[2:]
    drop = DropResourceService(session).get_drop_state()
    assert (drop.state, drop.owner_delivery_id) == (DropResourceState.FREE, None)


def test_already_recovered_is_idempotent_without_duplicate(session: Session) -> None:
    job, delivery, _ = _target(session)
    TerminalDropRecoveryService(session).recover(job_id=job.job_id, delivery_id=delivery.job_delivery_id)
    with pytest.raises(TerminalDropAlreadyRecovered):
        TerminalDropRecoveryService(session).recover(job_id=job.job_id, delivery_id=delivery.job_delivery_id)
    assert len(_returns(session, delivery.job_delivery_id)) == 1


def test_nonterminal_job_is_rejected(session: Session) -> None:
    job, delivery, _ = _target(session, job_status=JobStatus.RUNNING)
    with pytest.raises(TerminalDropRecoveryError, match="terminal ProductionJob"):
        TerminalDropRecoveryService(session).recover(job_id=job.job_id, delivery_id=delivery.job_delivery_id)
    assert _returns(session, delivery.job_delivery_id) == []


def test_drop_free_is_rejected(session: Session) -> None:
    job, delivery, _ = _target(session)
    returned = ExecutionAttempt(
        req_id="already-returned",
        executor_type=ExecutorType.FORKLIFT,
        command_type=EMPTY_RETURN_COMMAND_TYPE,
        job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id,
        attempt_no=1,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
    )
    session.add(returned)
    session.commit()
    with pytest.raises(TerminalDropAlreadyRecovered):
        TerminalDropRecoveryService(session).assess(job_id=job.job_id, delivery_id=delivery.job_delivery_id)


def test_drop_owned_by_another_delivery_is_rejected(session: Session) -> None:
    job, delivery, _ = _target(session)
    _, other_delivery, _ = _target(session, group="OUTER_WALLS")
    # First release the target delivery; the other successful forward then owns DROP.
    first_return = ExecutionAttempt(
        req_id="free-first-drop", executor_type=ExecutorType.FORKLIFT,
        command_type=EMPTY_RETURN_COMMAND_TYPE, job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id, attempt_no=1,
        status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
    )
    session.add(first_return)
    session.commit()
    assert DropResourceService(session).get_drop_state().owner_delivery_id == other_delivery.job_delivery_id
    with pytest.raises(TerminalDropRecoveryError):
        TerminalDropRecoveryService(session).assess(job_id=job.job_id, delivery_id=delivery.job_delivery_id)


def test_active_empty_return_is_rejected(session: Session) -> None:
    job, delivery, _ = _target(session)
    active = ExecutionAttempt(
        req_id="active-return", executor_type=ExecutorType.FORKLIFT,
        command_type=EMPTY_RETURN_COMMAND_TYPE, job_id=job.job_id,
        job_delivery_id=delivery.job_delivery_id, attempt_no=1,
        status=ExecutionAttemptStatus.CREATED,
        request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": "RACK2"}),
    )
    session.add(active)
    session.commit()
    with pytest.raises(TerminalDropRecoveryError, match="already active"):
        TerminalDropRecoveryService(session).recover(job_id=job.job_id, delivery_id=delivery.job_delivery_id)


@pytest.mark.parametrize(
    ("url", "actual"),
    [
        ("postgresql+psycopg://u:p@localhost:5432/smart_factory_db", "smart_factory_db"),
        ("postgresql+psycopg://u:p@localhost:5432/other", "other"),
        ("postgresql+psycopg://u:p@localhost:5432/smart_factory_benchmark", "smart_factory_db"),
    ],
)
def test_script_identity_guard_rejects_non_benchmark(url: str, actual: str) -> None:
    with pytest.raises(RuntimeError, match="BENCHMARK_TERMINAL_DROP_RECOVERY_ABORTED_WRONG_DATABASE"):
        _require_benchmark_identity(url=make_url(url), actual_database=actual)


def test_script_identity_guard_accepts_verified_benchmark() -> None:
    _require_benchmark_identity(
        url=make_url("postgresql+psycopg://u:p@localhost:5432/smart_factory_benchmark"),
        actual_database="smart_factory_benchmark",
    )


def test_cancelled_owner_with_unfinished_steps_is_recovered_only_by_explicit_test_override(session: Session) -> None:
    """Cancellation preserves DROP; terminal benchmark cleanup creates return evidence."""
    job, delivery, forward = _target(session, job_status=JobStatus.CANCELED, group="OUTER_WALLS")
    session.add_all([
        JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_DOOR_OUTER_WALL", display_name="Door", status=StepStatus.RUNNING),
        JobStep(job_id=job.job_id, step_order=2, operation_code="INSTALL_LEFT_OUTER_WALL", display_name="Left", status=StepStatus.PENDING),
    ])
    session.commit()
    assert DropResourceService(session).get_drop_state().owner_delivery_id == delivery.job_delivery_id
    assert _returns(session, delivery.job_delivery_id) == []

    next_job = ProductionJob(
        product_code="RECOVERY_PRODUCT", job_code="RECOVERY-NEXT", status=JobStatus.RUNNING
    )
    session.add(next_job)
    session.flush()
    next_delivery = JobMaterialDelivery(
        production_job_id=next_job.job_id, batch_order=1, delivery_code="RECOVERY-NEXT-OUTER",
        display_name="Next outer", status=MaterialDeliveryStatus.PENDING,
        supply_mode=SupplyMode.TRANSPORTED, supply_group_code="OUTER_WALLS",
        supply_destination_code="DROP",
    )
    session.add(next_delivery)
    session.commit()
    with pytest.raises(_TestOverrideTransportError, match="DROP"):
        _TestOverrideTransportService(session).complete_pending_delivery(
            production_job_id=next_job.job_id, job_delivery_id=next_delivery.job_delivery_id
        )
    assert session.get(JobMaterialDelivery, next_delivery.job_delivery_id).status is MaterialDeliveryStatus.PENDING

    notifications: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: notifications.append((job_id, reason)))
    try:
        override = TestOverrideService(
            session, enabled=True, database_name_resolver=lambda: "smart_factory_benchmark"
        )
        result = override.recover_terminal_drop_for_test_override(
            job_id=job.job_id, job_delivery_id=delivery.job_delivery_id
        )
        assert result.already_recovered is False
        assert result.attempt_id is not None
        assert len(_returns(session, delivery.job_delivery_id)) == 1
        assert _returns(session, delivery.job_delivery_id)[0].status is ExecutionAttemptStatus.SUCCEEDED
        assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
        # The blocked next Job can now use the ordinary Test Override transport path.
        completed_next = _TestOverrideTransportService(session).complete_pending_delivery(
            production_job_id=next_job.job_id, job_delivery_id=next_delivery.job_delivery_id
        )
        assert completed_next.delivery.status is MaterialDeliveryStatus.COMPLETED
        assert DropResourceService(session).get_drop_state().owner_delivery_id == next_delivery.job_delivery_id
        # A retry is safe and neither deletes nor duplicates the forward/return history.
        repeat = override.recover_terminal_drop_for_test_override(
            job_id=job.job_id, job_delivery_id=delivery.job_delivery_id
        )
        assert repeat.already_recovered is True
        assert len(_returns(session, delivery.job_delivery_id)) == 1
        assert session.get(ExecutionAttempt, forward.attempt_id).status is ExecutionAttemptStatus.SUCCEEDED
        assert notifications == [
            (job.job_id, "terminal_drop_recovery_completed"),
            (next_job.job_id, "material_delivery_completed"),
        ]
    finally:
        set_production_change_callback(None)
