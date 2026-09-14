from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.automatic_return_home_service import (
    AutomaticReturnHomeService,
    RETURN_HOME_COMMAND_TYPE,
)
from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
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
)
from shared.services.drop_resource_service import DropResourceService, DropResourceState
from shared.services.execution_attempt_service import ExecutionAttemptService


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Session:
    with session_factory() as db:
        yield db


def _coordinator_factory(transport: FakeForkliftActionTransport):
    def factory(session: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(
            session,
            adapter=ForkliftActionAdapter(transport),
            execution_attempt_service=ExecutionAttemptService(session),
        )
    return factory


def _add_delivery_lifecycle(
    session: Session,
    *,
    job_id: int,
    group: str,
    rack: str,
    empty_return_status: ExecutionAttemptStatus = ExecutionAttemptStatus.SUCCEEDED,
) -> JobMaterialDelivery:
    delivery = JobMaterialDelivery(
        production_job_id=job_id,
        batch_order=1 if group == "INNER_WALL" else 2,
        delivery_code=f"{group}-DELIVERY",
        display_name=group,
        status=MaterialDeliveryStatus.COMPLETED,
        supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code=group,
        supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    session.add_all((
        ExecutionAttempt(
            req_id=f"{group}-material",
            executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT",
            job_id=job_id,
            job_delivery_id=delivery.job_delivery_id,
            attempt_no=1,
            status=ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json=json.dumps({"pickup_code": rack, "dropoff_code": "DROP"}),
        ),
        ExecutionAttempt(
            req_id=f"{group}-empty-return",
            executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT_EMPTY_RETURN",
            job_id=job_id,
            job_delivery_id=delivery.job_delivery_id,
            attempt_no=1,
            status=empty_return_status,
            request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": rack}),
        ),
    ))
    return delivery


def _job(session: Session, *, include_outer: bool = True, outer_return_status: ExecutionAttemptStatus = ExecutionAttemptStatus.SUCCEEDED):
    product = Product(product_code="RETURN_HOME_TEST", product_name="ReturnHome test")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=product.product_code, job_code="RETURN-HOME-JOB", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    _add_delivery_lifecycle(session, job_id=job.job_id, group="INNER_WALL", rack="RACK2")
    if include_outer:
        _add_delivery_lifecycle(
            session,
            job_id=job.job_id,
            group="OUTER_WALLS",
            rack="RACK1",
            empty_return_status=outer_return_status,
        )
    session.commit()
    return job


def _service(session: Session, transport: FakeForkliftActionTransport) -> AutomaticReturnHomeService:
    return AutomaticReturnHomeService(
        session,
        forklift_execution_coordinator_factory=_coordinator_factory(transport),
        runtime_enabled=True,
    )


def _home_attempts(session: Session, job_id: int) -> list[ExecutionAttempt]:
    return list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.job_id == job_id,
        ExecutionAttempt.command_type == RETURN_HOME_COMMAND_TYPE,
    )))


def test_inner_return_does_not_home_while_outer_logistics_is_incomplete(session: Session) -> None:
    job = _job(session)
    outer = session.scalar(select(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id,
        JobMaterialDelivery.supply_group_code == "OUTER_WALLS",
    ))
    assert outer is not None
    session.query(ExecutionAttempt).filter(
        ExecutionAttempt.job_delivery_id == outer.job_delivery_id
    ).delete()
    outer.status = MaterialDeliveryStatus.PENDING
    session.commit()
    transport = FakeForkliftActionTransport()

    assert _service(session, transport).dispatch_one_eligible_return_home() is False
    assert transport.return_home_requests == []


def test_outer_material_arrival_without_empty_return_blocks_home(session: Session) -> None:
    job = _job(session)
    outer = session.scalar(select(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id,
        JobMaterialDelivery.supply_group_code == "OUTER_WALLS",
    ))
    assert outer is not None
    session.query(ExecutionAttempt).filter(
        ExecutionAttempt.job_delivery_id == outer.job_delivery_id,
        ExecutionAttempt.command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN",
    ).delete()
    session.commit()
    transport = FakeForkliftActionTransport()

    assert DropResourceService(session).get_drop_state().state is DropResourceState.OCCUPIED
    assert _service(session, transport).dispatch_one_eligible_return_home() is False
    assert transport.return_home_requests == []


def test_return_home_waits_for_outer_empty_return_and_drop_free(session: Session) -> None:
    job = _job(session, outer_return_status=ExecutionAttemptStatus.DISPATCHING)
    transport = FakeForkliftActionTransport()

    assert DropResourceService(session).get_drop_state().state is DropResourceState.RETURNING
    assert _service(session, transport).dispatch_one_eligible_return_home() is False
    assert transport.return_home_requests == []
    assert _home_attempts(session, job.job_id) == []


def test_all_transport_and_empty_return_success_dispatches_home_exactly_once(session: Session) -> None:
    job = _job(session)
    transport = FakeForkliftActionTransport()

    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
    assert _service(session, transport).dispatch_one_eligible_return_home() is True
    assert _service(session, transport).dispatch_one_eligible_return_home() is False

    attempts = _home_attempts(session, job.job_id)
    assert len(transport.return_home_requests) == len(attempts) == 1
    assert attempts[0].status is ExecutionAttemptStatus.SUCCEEDED
    assert attempts[0].job_id == job.job_id
    assert attempts[0].request_payload_json == json.dumps({"job_id": job.job_id}, separators=(",", ":"))


def test_restart_safe_service_reconciles_missing_return_home(session: Session) -> None:
    job = _job(session)
    transport = FakeForkliftActionTransport()

    # No callback-memory state is supplied to this new service instance.
    fresh_service = _service(session, transport)
    assert fresh_service.dispatch_one_eligible_return_home() is True
    assert len(transport.return_home_requests) == 1
    assert len(_home_attempts(session, job.job_id)) == 1


def test_failed_or_unknown_return_home_never_rolls_back_logistics_or_auto_retries(session: Session) -> None:
    job = _job(session)
    # ReturnHome is post-logistics and must never reopen a completed Job.
    session.get(ProductionJob, job.job_id).status = JobStatus.COMPLETED
    session.commit()
    failed_transport = FakeForkliftActionTransport(
        return_home_result=ForkliftExecutionResult(
            status=ForkliftActionStatus.FAILED,
            error_code="HOME_FAILED",
            detail="fake home failure",
        )
    )

    assert _service(session, failed_transport).dispatch_one_eligible_return_home() is True
    attempts = _home_attempts(session, job.job_id)
    assert len(attempts) == 1 and attempts[0].status is ExecutionAttemptStatus.FAILED
    assert session.get(ProductionJob, job.job_id).status is JobStatus.COMPLETED
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE
    assert _service(session, failed_transport).dispatch_one_eligible_return_home() is False
    assert len(failed_transport.return_home_requests) == 1

    attempts[0].status = ExecutionAttemptStatus.UNKNOWN
    session.commit()
    fresh_transport = FakeForkliftActionTransport()
    assert _service(session, fresh_transport).dispatch_one_eligible_return_home() is False
    assert fresh_transport.return_home_requests == []
