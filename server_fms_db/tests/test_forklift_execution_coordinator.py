import pytest
from sqlalchemy.orm import Session

from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter, ForkliftActionStatus
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.models.factory import ExecutionAttemptStatus
from shared.services.execution_attempt_service import ExecutionAttemptService


from shared.models import Base

@pytest.fixture
def session() -> Session:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    from shared.models.factory import Product
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_forklift_execute_transport_records_execution_attempt(session: Session) -> None:
    from shared.models.factory import ProductionJob, JobMaterialDelivery, JobStatus, MaterialDeliveryStatus
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(production_job_id=job.job_id, status=MaterialDeliveryStatus.PENDING, batch_order=1, delivery_code="D1", display_name="D1")
    session.add(delivery)
    session.flush()

    transport = FakeForkliftActionTransport()
    adapter = ForkliftActionAdapter(transport)
    attempt_service = ExecutionAttemptService(session)
    coordinator = ForkliftExecutionCoordinator(session, adapter=adapter, execution_attempt_service=attempt_service)

    result = coordinator.execute_transport(
        req_id="TEST-FL-1",
        job_id=job.job_id,
        delivery_id=delivery.job_delivery_id,
        pickup_code="RACK1",
        dropoff_code="DROP",
    )

    assert result.status == ForkliftActionStatus.SUCCEEDED

    # Check DB
    attempt = attempt_service.get_by_req_id("TEST-FL-1")
    assert attempt is not None
    assert attempt.executor_type.value == "FORKLIFT"
    assert attempt.command_type == "EXECUTE_TRANSPORT"
    assert attempt.status == ExecutionAttemptStatus.SUCCEEDED
    assert attempt.job_id == job.job_id
    assert attempt.job_delivery_id == delivery.job_delivery_id
    assert attempt.attempt_no == 1

def test_forklift_return_home_records_execution_attempt(session: Session) -> None:
    transport = FakeForkliftActionTransport()
    adapter = ForkliftActionAdapter(transport)
    attempt_service = ExecutionAttemptService(session)
    coordinator = ForkliftExecutionCoordinator(session, adapter=adapter, execution_attempt_service=attempt_service)

    result = coordinator.return_home(req_id="TEST-FL-HOME")

    assert result.status == ForkliftActionStatus.SUCCEEDED

    attempt = attempt_service.get_by_req_id("TEST-FL-HOME")
    assert attempt is not None
    assert attempt.executor_type.value == "FORKLIFT"
    assert attempt.command_type == "RETURN_HOME"
    assert attempt.status == ExecutionAttemptStatus.SUCCEEDED
    assert attempt.job_id is None
    assert attempt.attempt_no == 1
