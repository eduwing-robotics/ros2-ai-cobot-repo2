from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.cleanup_benchmark_stale_jobs import (
    BenchmarkCleanupSafetyError,
    cancel_selected_jobs,
    list_stale_running_jobs,
    main,
    require_benchmark_database_name,
)
from shared.models import Base
from shared.models.factory import (
    EventType,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobStatus,
    JobStep,
    Product,
    ProductionEvent,
    ProductionJob,
    StepStatus,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _running_job(
    session: Session,
    *,
    job_id_hint: str,
    requested_at: datetime,
    vision_class: str | None,
) -> ProductionJob:
    product = session.get(Product, f"PRODUCT_{job_id_hint}")
    if product is None:
        product = Product(product_code=f"PRODUCT_{job_id_hint}", product_name="Fixture product")
        session.add(product)
        session.flush()
    job = ProductionJob(
        job_code=f"JOB_{job_id_hint}",
        product_code=product.product_code,
        status=JobStatus.RUNNING,
        requested_at=requested_at,
    )
    session.add(job)
    session.flush()
    session.add(
        JobStep(
            job_id=job.job_id,
            step_order=1,
            operation_code="STEP_ALPHA",
            display_name="Step Alpha",
            vision_class=vision_class,
            status=StepStatus.PENDING,
        )
    )
    session.commit()
    return job


def test_database_guard_rejects_production_and_accepts_benchmark() -> None:
    require_benchmark_database_name("smart_factory_benchmark")

    with pytest.raises(BenchmarkCleanupSafetyError, match="smart_factory_db"):
        require_benchmark_database_name("smart_factory_db")


def test_dry_run_candidate_listing_is_read_only_and_selector_is_narrow(session: Session) -> None:
    now = datetime.now(timezone.utc)
    stale = _running_job(
        session,
        job_id_hint="STALE",
        requested_at=now - timedelta(days=3),
        vision_class=None,
    )
    fresh = _running_job(
        session,
        job_id_hint="FRESH",
        requested_at=now,
        vision_class="fixture_class",
    )

    candidates = list_stale_running_jobs(session, before=now - timedelta(days=1))

    assert [candidate.job_id for candidate in candidates] == [stale.job_id]
    assert candidates[0].pending_step_count == 1
    assert candidates[0].missing_vision_class_count == 1
    assert session.get(ProductionJob, stale.job_id).status is JobStatus.RUNNING
    assert session.get(ProductionJob, fresh.job_id).status is JobStatus.RUNNING


def test_explicit_cancellation_preserves_children_and_excludes_stale_job_from_fms_status_set(
    session: Session,
) -> None:
    now = datetime.now(timezone.utc)
    stale = _running_job(
        session,
        job_id_hint="STALE",
        requested_at=now - timedelta(days=3),
        vision_class=None,
    )
    fresh = _running_job(
        session,
        job_id_hint="FRESH",
        requested_at=now,
        vision_class="fixture_class",
    )
    candidates = list_stale_running_jobs(session, job_ids=[stale.job_id])

    assert cancel_selected_jobs(session, candidates, reason="fixture stale quarantine") == [stale.job_id]

    canceled = session.get(ProductionJob, stale.job_id)
    assert canceled is not None
    assert canceled.status is JobStatus.CANCELED
    assert session.scalar(select(JobStep.status).where(JobStep.job_id == stale.job_id)) is StepStatus.PENDING
    assert session.scalar(
        select(ProductionEvent.event_type).where(ProductionEvent.job_id == stale.job_id)
    ) is EventType.JOB_CANCELED

    # This is the status predicate used by FmsWorker._tick_in_session().
    fms_runnable_statuses = {
        JobStatus.REQUESTED,
        JobStatus.READY,
        JobStatus.RUNNING,
        JobStatus.PRE_ROOF_READY,
        JobStatus.ROOF_READY,
    }
    runnable_ids = list(
        session.scalars(
            select(ProductionJob.job_id)
            .where(ProductionJob.status.in_(fms_runnable_statuses))
            .order_by(ProductionJob.job_id)
        )
    )
    assert stale.job_id not in runnable_ids
    assert fresh.job_id in runnable_ids


def test_apply_requires_a_narrow_explicit_selector() -> None:
    with pytest.raises(BenchmarkCleanupSafetyError, match="requires at least one explicit selector"):
        main(["--apply"])


def test_apply_refuses_job_with_unresolved_execution_attempt(session: Session) -> None:
    stale = _running_job(
        session,
        job_id_hint="ACTIVE",
        requested_at=datetime.now(timezone.utc) - timedelta(days=3),
        vision_class=None,
    )
    session.add(
        ExecutionAttempt(
            req_id="fixture-active-attempt",
            executor_type=ExecutorType.ROBOT_CELL,
            command_type="EXECUTE_STEP",
            job_id=stale.job_id,
            attempt_no=1,
            status=ExecutionAttemptStatus.CREATED,
            request_payload_json="{}",
        )
    )
    session.commit()

    candidates = list_stale_running_jobs(session, job_ids=[stale.job_id])

    assert candidates[0].active_attempt_count == 1
    with pytest.raises(BenchmarkCleanupSafetyError, match="unresolved execution attempt"):
        cancel_selected_jobs(session, candidates, reason="fixture stale quarantine")
    assert session.get(ProductionJob, stale.job_id).status is JobStatus.RUNNING
