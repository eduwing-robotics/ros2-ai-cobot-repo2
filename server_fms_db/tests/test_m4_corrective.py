import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobStep,
    JobMaterialDelivery,
    ProductionJob,
    JobStatus,
    MaterialDeliveryStatus,
    StepStatus,
)
from shared.models import Base
from shared.services.execution_attempt_service import ExecutionAttemptService

from sqlalchemy import create_engine
import sqlalchemy.pool
@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        poolclass=sqlalchemy.pool.StaticPool,
        connect_args={'check_same_thread': False}
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    # Named shared-memory SQLite survives across test runs unless explicitly reset.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as _session:
        yield _session
    Base.metadata.drop_all(engine)
    engine.dispose()

def _setup_test_data(session: Session):
    from shared.models.factory import Product
    import uuid
    code = str(uuid.uuid4())[:8]
    product = Product(product_code=code, product_name=f"House {code}")
    session.add(product)
    session.flush()
    job = ProductionJob(product_code=code, job_code=f"J-{code}", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, operation_code="ROOF_INSTALL", step_order=1, status=StepStatus.PENDING, display_name="Roof Install")
    session.add(step)
    session.flush()
    delivery = JobMaterialDelivery(production_job_id=job.job_id, status=MaterialDeliveryStatus.PENDING, batch_order=1, delivery_code=f"D-{code}", display_name="D1")
    session.add(delivery)
    session.flush()
    return job.job_id, step.job_step_id, delivery.job_delivery_id

def test_durable_attempt_exists_before_transport_send(session: Session) -> None:
    job_id, job_step_id, delivery_id = _setup_test_data(session)
    session.commit()

    engine = session.get_bind()

    attempt_service = ExecutionAttemptService(session)
    attempt = attempt_service.create_attempt(
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="TEST_COMMAND",
        request_payload={"data": "test"},
        job_step_id=job_step_id,
        job_id=job_id
    )
    req_id = attempt.req_id
    attempt_service.mark_dispatching(req_id)

    # Simulate a crash: the main transaction is rolled back (e.g. error during fake action send)
    session.rollback()

    # In a NEW session, we must still be able to find the attempt in DISPATCHING state.
    new_session = sessionmaker(bind=engine)()
    try:
        durable_attempt = new_session.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id)
        )
        assert durable_attempt is not None
        assert durable_attempt.status == ExecutionAttemptStatus.DISPATCHING
    finally:
        new_session.close()
