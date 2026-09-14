from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.production_lifecycle import (
    NoExecutableStepError,
    ProductionStepLifecycleCoordinator,
)
from fms_server.step_executor import StepExecutionRequest, StepExecutionResult
from shared.models import Base
from tests.recipe_test_support import add_active_recipe
from shared.models.factory import (
    EventType,
    JobStatus,
    JobStep,
    Product,
    ProductionEvent,
    ProductionJob,
    StepStatus,
)
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionOrchestrationService,
)


PROCESS_STEPS = [f"TEST_OPERATION_{index:02d}" for index in range(1, 17)]


class FakeStepExecutor:
    """Test-only executor with predetermined terminal outcomes and no I/O."""

    def __init__(self, results: list[StepExecutionResult]) -> None:
        self._results = list(results)
        self.requests: list[StepExecutionRequest] = []

    def execute(self, request: StepExecutionRequest) -> StepExecutionResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("FakeStepExecutor received more requests than configured outcomes.")
        return self._results.pop(0)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    db_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db_session
    finally:
        db_session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def seeded_product(session: Session) -> Product:
    product = Product(product_code="LIFECYCLE_HOUSE", product_name="Lifecycle test house")
    session.add(product)
    session.flush()
    add_active_recipe(session, product.product_code)
    session.commit()
    return product


@pytest.fixture
def orchestration(session: Session) -> ProductionOrchestrationService:
    return ProductionOrchestrationService(session)


def _create_running_job(
    orchestration: ProductionOrchestrationService,
    product: Product,
    *,
    job_code: str = "LIFECYCLE-JOB-001",
) -> ProductionJob:
    job = orchestration.create_job(product_code=product.product_code, job_code=job_code)
    return orchestration.start_job(job.job_id)


def _ordered_steps(session: Session, job_id: int) -> list[JobStep]:
    return list(
        session.scalars(
            select(JobStep)
            .where(JobStep.job_id == job_id)
            .order_by(JobStep.step_order)
        )
    )


def _event_types(session: Session, job_id: int) -> list[EventType]:
    return list(
        session.scalars(
            select(ProductionEvent.event_type)
            .where(ProductionEvent.job_id == job_id)
            .order_by(ProductionEvent.event_id)
        )
    )


def test_fake_success_completes_s1_and_advances_to_s2(
    session: Session,
    orchestration: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = _create_running_job(orchestration, seeded_product)
    executor = FakeStepExecutor([StepExecutionResult.success()])
    coordinator = ProductionStepLifecycleCoordinator(orchestration, executor)

    outcome = coordinator.execute_next_step(job.job_id)

    assert outcome.execution.succeeded is True
    assert outcome.request.job_id == job.job_id
    assert outcome.request.job_step_id == outcome.job_step.job_step_id
    assert outcome.request.step_code == "TEST_OPERATION_01"
    assert outcome.job_step.status is StepStatus.COMPLETED
    assert executor.requests == [outcome.request]
    next_step = orchestration.get_next_step(job.job_id)
    assert next_step is not None and next_step.resolved_step_code == "TEST_OPERATION_02"
    assert _event_types(session, job.job_id) == [
        EventType.JOB_CREATED,
        EventType.JOB_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
    ]


def test_fake_failure_fails_s1_and_owning_job(
    session: Session,
    orchestration: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = _create_running_job(orchestration, seeded_product)
    executor = FakeStepExecutor([StepExecutionResult.failed("fake equipment failure")])
    coordinator = ProductionStepLifecycleCoordinator(orchestration, executor)

    outcome = coordinator.execute_next_step(job.job_id)

    stored_job = session.get(ProductionJob, job.job_id)
    assert outcome.execution.succeeded is False
    assert outcome.job_step.status is StepStatus.FAILED
    assert outcome.job_step.failure_reason == "fake equipment failure"
    assert stored_job is not None and stored_job.status is JobStatus.FAILED
    assert stored_job.failure_reason == "fake equipment failure"
    assert _event_types(session, job.job_id)[-2:] == [
        EventType.STEP_FAILED,
        EventType.JOB_FAILED,
    ]


def test_nine_fake_successes_complete_the_job(
    session: Session,
    orchestration: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = _create_running_job(orchestration, seeded_product)
    executor = FakeStepExecutor([StepExecutionResult.success() for _ in PROCESS_STEPS])
    coordinator = ProductionStepLifecycleCoordinator(orchestration, executor)

    outcomes = [coordinator.execute_next_step(job.job_id) for _ in PROCESS_STEPS]

    stored_job = session.get(ProductionJob, job.job_id)
    assert [outcome.request.step_code for outcome in outcomes] == PROCESS_STEPS
    assert all(outcome.job_step.status is StepStatus.COMPLETED for outcome in outcomes)
    assert stored_job is not None and stored_job.status is JobStatus.COMPLETED
    assert len(executor.requests) == 16
    assert _event_types(session, job.job_id)[-2:] == [EventType.STEP_COMPLETED, EventType.JOB_COMPLETED]


def test_failed_and_completed_jobs_do_not_invoke_an_executor_again(
    session: Session,
    orchestration: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    failed_job = _create_running_job(orchestration, seeded_product, job_code="LIFECYCLE-FAILED")
    failed_executor = FakeStepExecutor([StepExecutionResult.failed("planned failure")])
    failed_coordinator = ProductionStepLifecycleCoordinator(orchestration, failed_executor)
    failed_coordinator.execute_next_step(failed_job.job_id)
    with pytest.raises(InvalidProductionStateTransitionError):
        failed_coordinator.execute_next_step(failed_job.job_id)
    assert len(failed_executor.requests) == 1

    completed_job = _create_running_job(orchestration, seeded_product, job_code="LIFECYCLE-COMPLETED")
    completed_executor = FakeStepExecutor([StepExecutionResult.success() for _ in PROCESS_STEPS])
    completed_coordinator = ProductionStepLifecycleCoordinator(orchestration, completed_executor)
    for _ in PROCESS_STEPS:
        completed_coordinator.execute_next_step(completed_job.job_id)
    with pytest.raises(NoExecutableStepError):
        completed_coordinator.execute_next_step(completed_job.job_id)
    assert len(completed_executor.requests) == 16


def test_existing_orchestration_rejects_out_of_order_step_before_execution(
    session: Session,
    orchestration: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = _create_running_job(orchestration, seeded_product)
    second_step = _ordered_steps(session, job.job_id)[1]
    executor = FakeStepExecutor([StepExecutionResult.success()])

    with pytest.raises(InvalidProductionStateTransitionError):
        orchestration.start_step(second_step.job_step_id)

    assert session.get(JobStep, second_step.job_step_id).status is StepStatus.PENDING
    assert executor.requests == []
