from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.forklift_action_adapter import (
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftActionTransport,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
)
from shared.services.execution_attempt_service import ExecutionAttemptService


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class ObservingTransport(ForkliftActionTransport):
    def __init__(self, engine, *, result: ForkliftExecutionResult) -> None:
        self._engine = engine
        self._result = result
        self.observed_status: ExecutionAttemptStatus | None = None

    def send_execute_transport(self, req_id, job_id, delivery_id, pickup_code, dropoff_code, feedback_callback=None):
        with Session(self._engine) as observer:
            attempt = observer.scalar(select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id))
            assert attempt is not None
            self.observed_status = attempt.status
        return self._result

    def send_return_home(self, req_id, feedback_callback=None):
        raise AssertionError("not used")


@pytest.fixture
def setup(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'attempt_durability.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        session.add(Product(product_code="DURABILITY_PRODUCT", product_name="Durability product"))
        session.flush()
        job = ProductionJob(job_code="DURABILITY_JOB", product_code="DURABILITY_PRODUCT", status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id, batch_order=1, delivery_code="DURABILITY_DEL", display_name="Durability delivery",
            status=MaterialDeliveryStatus.IN_PROGRESS,
        )
        session.add(delivery)
        session.commit()
        yield engine, factory, job.job_id, delivery.job_delivery_id
    engine.dispose()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ForkliftExecutionResult(ForkliftActionStatus.SUCCEEDED, "", "ok"), ExecutionAttemptStatus.SUCCEEDED),
        (ForkliftExecutionResult(ForkliftActionStatus.FAILED, "FAKE", "failed"), ExecutionAttemptStatus.FAILED),
        (ForkliftExecutionResult(cast(ForkliftActionStatus, _Status("UNKNOWN")), "", "unknown"), ExecutionAttemptStatus.UNKNOWN),
    ],
)
def test_forklift_attempt_state_is_durable_before_adapter_and_after_terminal_result(setup, result, expected) -> None:
    engine, factory, job_id, delivery_id = setup
    transport = ObservingTransport(engine, result=result)
    with factory() as session:
        coordinator = ForkliftExecutionCoordinator(
            session,
            adapter=ForkliftActionAdapter(transport),
            execution_attempt_service=ExecutionAttemptService(session),
        )
        coordinator.execute_transport(
            req_id=f"durable-{expected.value}", job_id=job_id, delivery_id=delivery_id,
            pickup_code="RACK1", dropoff_code="DROP",
        )
        # No outer session.commit() occurs before this fresh observer read.
        with Session(engine) as observer:
            attempt = observer.scalar(select(ExecutionAttempt).where(ExecutionAttempt.req_id == f"durable-{expected.value}"))
            assert attempt is not None and attempt.status is expected
    assert transport.observed_status is ExecutionAttemptStatus.DISPATCHING
