from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from tests.recipe_test_support import add_active_recipe
from shared.models.factory import (
    EventType,
    JobStatus,
    JobStep,
    Product,
    AssemblyRecipe,
    ProductionEvent,
    ProductionJob,
    RoofOptionCode,
    StepStatus,
)
from shared.services.assembly_recipe_service import ActiveAssemblyRecipeNotFoundError
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
        ProductionOrchestrationService,
    ProductNotFoundError,
    InvalidProductionInputError,
)

PROCESS_STEPS = [f"TEST_OPERATION_{index:02d}" for index in range(1, 17)]


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        engine.dispose()


@pytest.fixture
def seeded_product(session: Session) -> Product:
    product = Product(product_code="HOUSE_TEST", product_name="Test House")
    session.add(product)
    session.flush()
    add_active_recipe(session, product.product_code)
    session.commit()
    return product


@pytest.fixture
def service(session: Session) -> ProductionOrchestrationService:
    return ProductionOrchestrationService(session)


def ordered_steps(session: Session, job_id: int) -> list[JobStep]:
    return list(
        session.scalars(
            select(JobStep)
            .where(JobStep.job_id == job_id)
            .order_by(JobStep.step_order)
        )
    )


def event_types(session: Session, job_id: int) -> list[EventType]:
    return list(
        session.scalars(
            select(ProductionEvent.event_type)
            .where(ProductionEvent.job_id == job_id)
            .order_by(ProductionEvent.event_id)
        )
    )


def create_job(service: ProductionOrchestrationService, product: Product, *, code: str = "JOB-001") -> ProductionJob:
    return service.create_job(product_code=product.product_code, job_code=code)


def test_create_job_initializes_all_recipe_stages_in_database_order(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)

    assert job.status is JobStatus.REQUESTED
    assert job.roof_option_code is None
    steps = ordered_steps(session, job.job_id)
    assert [step.resolved_step_code for step in steps] == PROCESS_STEPS
    assert [step.status for step in steps] == [StepStatus.PENDING] * 16
    assert event_types(session, job.job_id) == [EventType.JOB_CREATED]



@pytest.mark.parametrize("roof_option_code", [RoofOptionCode.ROOF_01, RoofOptionCode.ROOF_02])
def test_create_job_persists_supported_roof_option_code(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
    roof_option_code: RoofOptionCode,
) -> None:
    job = service.create_job(
        product_code=seeded_product.product_code,
        job_code=f"JOB-{roof_option_code.value}",
        roof_option_code=roof_option_code,
    )

    stored_job = session.get(ProductionJob, job.job_id)
    assert stored_job is not None
    assert stored_job.roof_option_code is roof_option_code
    assert [step.resolved_step_code for step in ordered_steps(session, job.job_id)] == PROCESS_STEPS
    assert event_types(session, job.job_id) == [EventType.JOB_CREATED]


@pytest.mark.parametrize("roof_option_code", ["ROOF_03", "FLAT", "1", 1])
def test_create_job_rejects_invalid_roof_option_code(
    service: ProductionOrchestrationService,
    seeded_product: Product,
    roof_option_code: object,
) -> None:
    with pytest.raises(InvalidProductionInputError, match="roof_option_code"):
        service.create_job(
            product_code=seeded_product.product_code,
            job_code="JOB-INVALID-ROOF",
            roof_option_code=roof_option_code,  # type: ignore[arg-type]
        )

def test_next_step_starts_at_s1_then_advances_to_s2(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None
    assert first_step.resolved_step_code == "TEST_OPERATION_01"

    service.start_job(job.job_id)
    service.start_step(first_step.job_step_id)
    service.complete_step(first_step.job_step_id)

    next_step = service.get_next_step(job.job_id)
    assert next_step is not None
    assert next_step.resolved_step_code == "TEST_OPERATION_02"
    assert session.get(JobStep, first_step.job_step_id).status is StepStatus.COMPLETED


def test_start_step_marks_running_and_records_events(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    service.start_job(job.job_id)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None

    service.start_step(first_step.job_step_id)

    stored_step = session.get(JobStep, first_step.job_step_id)
    assert stored_step.status is StepStatus.RUNNING
    assert stored_step.started_at is not None
    assert event_types(session, job.job_id) == [
        EventType.JOB_CREATED,
        EventType.JOB_STARTED,
        EventType.STEP_STARTED,
    ]


def test_fail_step_fails_owning_job_and_records_both_events(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    service.start_job(job.job_id)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None
    service.start_step(first_step.job_step_id)

    service.fail_step(first_step.job_step_id, reason="robot cell result failed")

    assert session.get(JobStep, first_step.job_step_id).status is StepStatus.FAILED
    stored_job = session.get(ProductionJob, job.job_id)
    assert stored_job.status is JobStatus.FAILED
    assert stored_job.failure_reason == "robot cell result failed"
    assert event_types(session, job.job_id) == [
        EventType.JOB_CREATED,
        EventType.JOB_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_FAILED,
        EventType.JOB_FAILED,
    ]
    step_failed = session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_id == job.job_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    )
    assert step_failed is not None and step_failed.error_code is None


def test_fail_step_records_optional_structured_error_code(session: Session, service: ProductionOrchestrationService, seeded_product: Product) -> None:
    job = create_job(service, seeded_product, code="JOB-ERROR-CODE")
    service.start_job(job.job_id)
    step = service.get_next_step(job.job_id)
    assert step is not None
    service.start_step(step.job_step_id)

    service.fail_step(step.job_step_id, reason="calibration mismatch", error_code="E503")

    event = session.scalar(
        select(ProductionEvent).where(
            ProductionEvent.job_step_id == step.job_step_id,
            ProductionEvent.event_type == EventType.STEP_FAILED,
        )
    )
    assert event is not None and event.error_code == "E503"
    assert session.get(JobStep, step.job_step_id).failure_reason == "calibration mismatch"


def test_completing_last_step_auto_completes_job(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    service.start_job(job.job_id)

    for _ in PROCESS_STEPS:
        step = service.get_next_step(job.job_id)
        assert step is not None
        service.start_step(step.job_step_id)
        service.complete_step(step.job_step_id)

    stored_job = session.get(ProductionJob, job.job_id)
    assert stored_job.status is JobStatus.COMPLETED
    assert stored_job.completed_at is not None
    assert service.get_next_step(job.job_id) is None
    assert event_types(session, job.job_id)[-2:] == [EventType.STEP_COMPLETED, EventType.JOB_COMPLETED]


def test_invalid_state_transitions_are_rejected(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    first_step = service.get_next_step(job.job_id)
    assert first_step is not None

    with pytest.raises(InvalidProductionStateTransitionError):
        service.start_step(first_step.job_step_id)
    with pytest.raises(InvalidProductionStateTransitionError):
        service.complete_job(job.job_id)

    service.start_job(job.job_id)
    with pytest.raises(InvalidProductionStateTransitionError):
        service.complete_step(first_step.job_step_id)

    service.start_step(first_step.job_step_id)
    service.complete_step(first_step.job_step_id)
    with pytest.raises(InvalidProductionStateTransitionError):
        service.start_step(first_step.job_step_id)

    assert session.get(JobStep, first_step.job_step_id).status is StepStatus.COMPLETED


def test_cannot_start_a_later_step_before_all_prior_steps_complete(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)
    service.start_job(job.job_id)
    second_step = ordered_steps(session, job.job_id)[1]

    with pytest.raises(InvalidProductionStateTransitionError):
        service.start_step(second_step.job_step_id)

    assert session.get(JobStep, second_step.job_step_id).status is StepStatus.PENDING
    assert event_types(session, job.job_id) == [EventType.JOB_CREATED, EventType.JOB_STARTED]


def test_fail_job_records_failure_event_without_changing_steps(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
) -> None:
    job = create_job(service, seeded_product)

    service.fail_job(job.job_id, reason="preflight failed")

    stored_job = session.get(ProductionJob, job.job_id)
    assert stored_job.status is JobStatus.FAILED
    assert stored_job.failure_reason == "preflight failed"
    assert all(step.status is StepStatus.PENDING for step in ordered_steps(session, job.job_id))
    assert event_types(session, job.job_id) == [EventType.JOB_CREATED, EventType.JOB_FAILED]


def test_event_write_failure_rolls_back_state_transition(
    session: Session,
    service: ProductionOrchestrationService,
    seeded_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = create_job(service, seeded_product)

    def fail_event_write(**kwargs: object) -> ProductionEvent:
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(service, "_record_event", fail_event_write)
    with pytest.raises(RuntimeError, match="event write failure"):
        service.start_job(job.job_id)

    session.expire_all()
    assert session.get(ProductionJob, job.job_id).status is JobStatus.REQUESTED
    assert event_types(session, job.job_id) == [EventType.JOB_CREATED]


def test_create_job_requires_existing_product_and_active_recipe(
    session: Session,
    service: ProductionOrchestrationService,
) -> None:
    with pytest.raises(ProductNotFoundError):
        service.create_job(product_code="999", job_code="MISSING-PRODUCT")

    product = Product(product_code="HOUSE_ONLY", product_name="House only")
    session.add(product)
    session.commit()
    with pytest.raises(ActiveAssemblyRecipeNotFoundError):
        service.create_job(product_code=product.product_code, job_code="MISSING-STEPS")
    assert session.scalar(select(ProductionJob).where(ProductionJob.job_code == "MISSING-STEPS")) is None
