from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.prepare_pre_roof_rehearsal_job import (
    EXPECTED_REVISION,
    PreparationError,
    _verify_revision,
    prepare_job_to_pre_roof_ready,
)
from scripts.pre_roof_full_stack_rehearsal import verify_job_is_safe
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE, seed_house_b_mvp_master
from tests.recipe_test_support import seed_inventory_for_recipe
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobStatus,
    JobStep,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionJob,
    RoofOptionCode,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield result
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_house_b_job(factory: sessionmaker[Session], *, code: str) -> ProductionJob:
    with factory() as session:
        recipe = seed_house_b_mvp_master(session)
        seed_inventory_for_recipe(session, recipe)
        session.commit()
        return ProductionOrchestrationService(session).create_job(
            product_code=HOUSE_B_PRODUCT_CODE,
            job_code=code,
            roof_option_code=RoofOptionCode.ROOF_02,
        )


def test_normal_fake_preparation_reaches_pre_roof_without_roof_or_inspection(factory: sessionmaker[Session]) -> None:
    job = create_house_b_job(factory, code="REHEARSAL-PREP-GOLDEN")
    output: list[str] = []

    result = prepare_job_to_pre_roof_ready(
        factory, job_id=job.job_id, timeout_seconds=10, emit=output.append
    )

    assert result.job_id == job.job_id
    assert len(result.dispatched_steps) == 6
    with factory() as session:
        stored = session.get(ProductionJob, job.job_id)
        assert stored is not None and stored.status is JobStatus.PRE_ROOF_READY
        assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id == job.job_id)) == 6
        inspections = list(session.scalars(select(ProductionInspection).where(
            ProductionInspection.production_job_id == job.job_id
        )))
        assert len(inspections) == 1
        assert inspections[0].status is ProductionInspectionStatus.PENDING
        transactions = list(session.scalars(select(IncomingQATransaction).where(
            IncomingQATransaction.production_job_id == job.job_id
        )))
        assert len(transactions) == 2
        assert all(row.status is IncomingQATransactionStatus.COMPLETED for row in transactions)
    assert any("Incoming QA     RELEASED" == item for item in output)
    # The existing process-boundary rehearsal target preflight accepts exactly
    # this state when no unrelated active job exists.
    assert verify_job_is_safe(factory, job_id=job.job_id) == (job.job_id, job.job_code)


def test_extra_active_job_fails_closed_before_fake_dispatch(factory: sessionmaker[Session]) -> None:
    target = create_house_b_job(factory, code="REHEARSAL-PREP-TARGET")
    create_house_b_job(factory, code="REHEARSAL-PREP-OTHER")

    with pytest.raises(PreparationError, match="FMS worker scans globally") as raised:
        prepare_job_to_pre_roof_ready(factory, job_id=target.job_id, timeout_seconds=2)

    assert raised.value.phase == "EXTRA_ACTIVE_JOB"
    with factory() as session:
        assert session.get(ProductionJob, target.job_id).status is JobStatus.REQUESTED


def test_nonrequested_target_is_rejected_without_state_reset(factory: sessionmaker[Session]) -> None:
    job = create_house_b_job(factory, code="REHEARSAL-PREP-RUNNING")
    with factory() as session:
        ProductionOrchestrationService(session).start_job(job.job_id)

    with pytest.raises(PreparationError, match="must be REQUESTED") as raised:
        prepare_job_to_pre_roof_ready(factory, job_id=job.job_id, timeout_seconds=2)

    assert raised.value.phase == "TARGET_JOB_INVALID"
    with factory() as session:
        assert session.get(ProductionJob, job.job_id).status is JobStatus.RUNNING


def test_revision_precheck_rejects_without_migration() -> None:
    class FakeSession:
        def scalar(self, _query):
            return "not-head"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeFactory:
        def __call__(self):
            return FakeSession()

    with pytest.raises(PreparationError, match=EXPECTED_REVISION) as raised:
        _verify_revision(FakeFactory())

    assert raised.value.phase == "ALEMBIC_REVISION_MISMATCH"
