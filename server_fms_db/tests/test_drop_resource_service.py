import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fms_server.empty_pallet_return_service import (
    EmptyPalletReturnNotEligibleError,
    EmptyPalletReturnService,
)
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
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
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    DropResourceService,
    DropResourceState,
)
from shared.services.execution_attempt_service import ExecutionAttemptService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(Product(product_code="DROP_TEST_PRODUCT", product_name="DROP test product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _delivery(session: Session, *, suffix: str, group: str, completed: bool = True) -> JobMaterialDelivery:
    job = ProductionJob(
        job_code=f"DROP_JOB_{suffix}", product_code="DROP_TEST_PRODUCT", status=JobStatus.RUNNING
    )
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"DROP_DEL_{suffix}",
        display_name=f"DROP delivery {suffix}",
        status=MaterialDeliveryStatus.COMPLETED if completed else MaterialDeliveryStatus.PENDING,
        supply_mode=SupplyMode.TRANSPORTED, supply_group_code=group,
        supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    return delivery


def _attempt(
    session: Session,
    delivery: JobMaterialDelivery,
    *,
    command_type: str,
    status: ExecutionAttemptStatus,
    attempt_no: int = 1,
) -> ExecutionAttempt:
    returning = command_type == EMPTY_RETURN_COMMAND_TYPE
    payload = {
        "job_id": delivery.production_job_id,
        "delivery_id": delivery.job_delivery_id,
        "pickup_code": "DROP" if returning else "RACK1",
        "dropoff_code": "RACK1" if returning else "DROP",
    }
    attempt = ExecutionAttempt(
        req_id=f"drop-{delivery.job_delivery_id}-{command_type}-{attempt_no}",
        executor_type=ExecutorType.FORKLIFT, command_type=command_type,
        job_id=delivery.production_job_id, job_delivery_id=delivery.job_delivery_id,
        attempt_no=attempt_no, status=status, request_payload_json=json.dumps(payload),
    )
    session.add(attempt)
    session.flush()
    return attempt


def _empty_return_service(session: Session) -> EmptyPalletReturnService:
    coordinator = ForkliftExecutionCoordinator(
        session, adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    return EmptyPalletReturnService(session, forklift_execution_coordinator=coordinator)


def test_drop_state_material_success_return_active_return_success(session: Session) -> None:
    delivery = _delivery(session, suffix="A", group="OUTER_WALLS")
    _attempt(session, delivery, command_type=MATERIAL_TRANSPORT_COMMAND_TYPE, status=ExecutionAttemptStatus.SUCCEEDED)
    session.commit()
    state = DropResourceService(session).get_drop_state()
    assert (state.state, state.owner_delivery_id) == (DropResourceState.OCCUPIED, delivery.job_delivery_id)

    returning = _attempt(
        session, delivery, command_type=EMPTY_RETURN_COMMAND_TYPE, status=ExecutionAttemptStatus.ACCEPTED
    )
    session.commit()
    assert DropResourceService(session).get_drop_state().state is DropResourceState.RETURNING

    returning.status = ExecutionAttemptStatus.SUCCEEDED
    session.commit()
    assert DropResourceService(session).get_drop_state().state is DropResourceState.FREE


@pytest.mark.parametrize("status", [
    ExecutionAttemptStatus.FAILED,
    ExecutionAttemptStatus.CANCELED,
    ExecutionAttemptStatus.UNKNOWN,
])
def test_unsuccessful_empty_return_never_releases_drop(session: Session, status: ExecutionAttemptStatus) -> None:
    delivery = _delivery(session, suffix=status.value, group="OUTER_WALLS")
    _attempt(session, delivery, command_type=MATERIAL_TRANSPORT_COMMAND_TYPE, status=ExecutionAttemptStatus.SUCCEEDED)
    _attempt(session, delivery, command_type=EMPTY_RETURN_COMMAND_TYPE, status=status)
    session.commit()

    state = DropResourceService(session).get_drop_state()
    assert state.state is (DropResourceState.RETURNING if status is ExecutionAttemptStatus.UNKNOWN else DropResourceState.OCCUPIED)
    assert state.owner_delivery_id == delivery.job_delivery_id


@pytest.mark.parametrize("status", [ExecutionAttemptStatus.FAILED, ExecutionAttemptStatus.CANCELED, ExecutionAttemptStatus.UNKNOWN])
def test_unresolved_material_result_reserves_drop_fail_closed(session: Session, status: ExecutionAttemptStatus) -> None:
    delivery = _delivery(session, suffix=f"MAT-{status.value}", group="INNER_WALL")
    _attempt(session, delivery, command_type=MATERIAL_TRANSPORT_COMMAND_TYPE, status=status)
    session.commit()

    state = DropResourceService(session).get_drop_state()
    assert state.state is DropResourceState.RESERVED
    assert state.owner_delivery_id == delivery.job_delivery_id


def test_empty_return_rejects_completed_delivery_that_does_not_own_drop(session: Session) -> None:
    owner = _delivery(session, suffix="OWNER", group="OUTER_WALLS")
    other = _delivery(session, suffix="OTHER", group="INNER_WALL")
    _attempt(session, owner, command_type=MATERIAL_TRANSPORT_COMMAND_TYPE, status=ExecutionAttemptStatus.SUCCEEDED)
    session.commit()

    with pytest.raises(EmptyPalletReturnNotEligibleError, match="current DROP owner"):
        _empty_return_service(session).execute_empty_pallet_return(job_delivery_id=other.job_delivery_id)

    assert DropResourceService(session).get_drop_state().owner_delivery_id == owner.job_delivery_id
