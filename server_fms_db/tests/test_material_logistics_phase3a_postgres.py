"""Guarded PostgreSQL Phase 3A policy readiness coverage."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.manual_prestage_service import ManualPrestageService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService


pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    if not test_url or make_url(test_url).database != EXPECTED_DATABASE:
        pytest.fail("Phase 3A PostgreSQL tests require guarded smart_factory_benchmark.")
    if settings.database_url.strip() and test_url == settings.database_url.strip():
        pytest.fail("POSTGRES_TEST_DATABASE_URL must not equal DATABASE_URL.")
    return test_url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_DATABASE
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def _job_with_step(session: Session, *, suffix: str, mode: SupplyMode | None, group: str | None) -> tuple[ProductionJob, JobStep, Part]:
    product = Product(product_code=f"P3APG_PRODUCT_{suffix}", product_name="Phase 3A PostgreSQL fixture")
    part = Part(
        part_code=f"P3APG_PART_{suffix}",
        part_name="Phase 3A part",
        category=PartCategory.STRUCTURE,
        vision_class="wall_ext_left",
        unit="EA",
    )
    session.add_all((product, part))
    session.flush()
    job = ProductionJob(job_code=f"P3APG_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_LEFT_OUTER_WALL",
        display_name="Install",
        part_code=part.part_code,
        quantity=1,
        supply_mode=mode,
        supply_group_code=group,
        is_terminal=True,
        status=StepStatus.PENDING,
    )
    session.add(step)
    session.flush()
    return job, step, part


def _cleanup_p3a_owned_rows(
    session_factory: sessionmaker[Session], *, created: list[tuple[int, str, str]]
) -> None:
    """Delete only committed P3APG fixture graphs, independent of assertions."""
    if not created:
        return
    job_ids = [row[0] for row in created]
    part_codes = [row[1] for row in created]
    product_codes = [row[2] for row in created]
    with session_factory() as session:
        delivery_ids = list(session.scalars(
            select(JobMaterialDelivery.job_delivery_id).where(
                JobMaterialDelivery.production_job_id.in_(job_ids)
            )
        ))
        step_ids = list(session.scalars(
            select(JobStep.job_step_id).where(JobStep.job_id.in_(job_ids))
        ))
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id.in_(job_ids)))
        if step_ids:
            session.execute(delete(ExecutionAttempt).where(
                ExecutionAttempt.job_step_id.in_(step_ids)
            ))
        if delivery_ids:
            item_ids = list(session.scalars(
                select(JobMaterialDeliveryItem.delivery_item_id).where(
                    JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
                )
            ))
            if item_ids:
                session.execute(delete(MaterialInspection).where(
                    MaterialInspection.delivery_item_id.in_(item_ids)
                ))
            session.execute(delete(JobMaterialFeedExecution).where(
                JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids)
            ))
            session.execute(delete(JobMaterialDeliveryItem).where(
                JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
            ))
            session.execute(delete(JobMaterialDelivery).where(
                JobMaterialDelivery.job_delivery_id.in_(delivery_ids)
            ))
        session.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids)))
        session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
        session.execute(delete(Part).where(Part.part_code.in_(part_codes)))
        session.execute(delete(Product).where(Product.product_code.in_(product_codes)))
        session.commit()


def _assert_p3a_owned_rows_absent(
    session_factory: sessionmaker[Session], *, created: list[tuple[int, str, str]]
) -> None:
    job_ids = [row[0] for row in created]
    part_codes = [row[1] for row in created]
    product_codes = [row[2] for row in created]
    with session_factory() as session:
        assert session.scalar(select(ProductionJob.job_id).where(
            ProductionJob.job_id.in_(job_ids)
        )) is None
        assert session.scalar(select(JobStep.job_step_id).where(
            JobStep.job_id.in_(job_ids),
            JobStep.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
        )) is None
        delivery_ids = select(JobMaterialDelivery.job_delivery_id).where(
            JobMaterialDelivery.production_job_id.in_(job_ids)
        )
        item_ids = select(JobMaterialDeliveryItem.delivery_item_id).where(
            JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
        )
        assert session.scalar(select(MaterialInspection.inspection_id).where(
            MaterialInspection.delivery_item_id.in_(item_ids),
            MaterialInspection.status.in_([MaterialInspectionStatus.REQUESTED, MaterialInspectionStatus.RUNNING]),
        )) is None
        assert session.scalar(select(ExecutionAttempt.attempt_id).where(
            ExecutionAttempt.job_id.in_(job_ids),
            ExecutionAttempt.status.in_([
                ExecutionAttemptStatus.CREATED,
                ExecutionAttemptStatus.DISPATCHING,
                ExecutionAttemptStatus.ACCEPTED,
                ExecutionAttemptStatus.UNKNOWN,
            ]),
        )) is None
        assert session.scalar(select(Product.product_code).where(
            Product.product_code.in_(product_codes)
        )) is None
        assert session.scalar(select(Part.part_code).where(Part.part_code.in_(part_codes))) is None


def test_policy_feed_applicability_and_readiness_fail_closed(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    created: list[tuple[int, str, str]] = []
    try:
        with session_factory() as session:
            transported_job, transported_step, transported_part = _job_with_step(
                session, suffix=f"{suffix}T", mode=SupplyMode.TRANSPORTED, group="TEST_P3A_TRANSPORT"
            )
            legacy_job, legacy_step, legacy_part = _job_with_step(
                session, suffix=f"{suffix}L", mode=None, group=None
            )
            manual_job, manual_step, manual_part = _job_with_step(
                session, suffix=f"{suffix}M", mode=SupplyMode.MANUAL, group="TEST_P3A_MANUAL"
            )
            session.commit()
            # Persist exact cleanup identities before any assertion can raise.
            created = [
                (transported_job.job_id, transported_part.part_code, transported_job.product_code),
                (legacy_job.job_id, legacy_part.part_code, legacy_job.product_code),
                (manual_job.job_id, manual_part.part_code, manual_job.product_code),
            ]
            transport_delivery = MaterialDeliveryService(session).instantiate_for_job(job=transported_job)[0]
            legacy_delivery = MaterialDeliveryService(session).instantiate_for_job(job=legacy_job)[0]
            manual_delivery = MaterialDeliveryService(session).instantiate_for_job(job=manual_job)[0]
            session.commit()
            assert session.scalar(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == transport_delivery.job_delivery_id)) is None
            assert session.scalar(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == manual_delivery.job_delivery_id)) is None
            assert session.scalar(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == legacy_delivery.job_delivery_id)) is not None

            transport_item = transport_delivery.items[0]
            transport_delivery.physical_ready_at = datetime.now(timezone.utc)
            transport_delivery.status = MaterialDeliveryStatus.COMPLETED
            session.add(
                MaterialInspection(
                    inspection_request_id=f"p3a-pg-qa-{suffix}",
                    delivery_item_id=transport_item.delivery_item_id,
                    inspection_cycle=1,
                    status=MaterialInspectionStatus.COMPLETED,
                    result=MaterialInspectionResult.PASS,
                    expected_part_code=transport_item.part_code,
                    expected_class_name=transported_part.vision_class,
                    expected_quantity=1,
                    detected_quantity=1,
                    production_valid=True,
                )
            )
            session.commit()
            readiness = StepReadinessService(MaterialDeliveryService(session))
            assert readiness.evaluate(job_id=transported_job.job_id, job_step_id=transported_step.job_step_id).ready is True
            assert readiness.evaluate(job_id=manual_job.job_id, job_step_id=manual_step.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
            manual_item = manual_delivery.items[0]
            session.add(
                MaterialInspection(
                    inspection_request_id=f"p3a-pg-manual-qa-{suffix}",
                    delivery_item_id=manual_item.delivery_item_id,
                    inspection_cycle=1,
                    status=MaterialInspectionStatus.COMPLETED,
                    result=MaterialInspectionResult.PASS,
                    expected_part_code=manual_item.part_code,
                    expected_class_name=manual_part.vision_class,
                    expected_quantity=1,
                    detected_quantity=1,
                    production_valid=True,
                )
            )
            session.commit()
            assert readiness.evaluate(job_id=manual_job.job_id, job_step_id=manual_step.job_step_id).reason is StepReadinessReason.MANUAL_PRESTAGE_REQUIRED
            ManualPrestageService(session).confirm_manual_prestage_ready(
                job_id=manual_job.job_id,
                job_delivery_id=manual_delivery.job_delivery_id,
                request_id=f"p3a-pg-manual-prestage-{suffix}",
            )
            assert readiness.evaluate(job_id=manual_job.job_id, job_step_id=manual_step.job_step_id).ready is True

            missing = JobStep(
                job_id=transported_job.job_id,
                step_order=2,
                operation_code="INSTALL_LEFT_OUTER_WALL",
                display_name="Missing policy delivery",
                part_code="P3APG_MISSING",
                quantity=1,
                supply_mode=SupplyMode.TRANSPORTED,
                supply_group_code="TEST_P3A_MISSING",
                status=StepStatus.PENDING,
            )
            session.add(missing)
            session.commit()
            assert readiness.evaluate(job_id=transported_job.job_id, job_step_id=missing.job_step_id).reason is StepReadinessReason.POLICY_INVALID
    finally:
        _cleanup_p3a_owned_rows(session_factory, created=created)
    _assert_p3a_owned_rows_absent(session_factory, created=created)


def test_policy_feed_cleanup_keeps_committed_variants_exception_safe(
    session_factory: sessionmaker[Session],
) -> None:
    """Regression for the former assertion-before-tracking pollution path."""
    suffix = uuid.uuid4().hex[:12].upper()
    created: list[tuple[int, str, str]] = []
    try:
        with session_factory() as session:
            variants = [
                _job_with_step(session, suffix=f"{suffix}T", mode=SupplyMode.TRANSPORTED, group="TEST_P3A_TRANSPORT"),
                _job_with_step(session, suffix=f"{suffix}L", mode=None, group=None),
                _job_with_step(session, suffix=f"{suffix}M", mode=SupplyMode.MANUAL, group="TEST_P3A_MANUAL"),
            ]
            session.commit()
            created = [(job.job_id, part.part_code, job.product_code) for job, _step, part in variants]
            raise RuntimeError("controlled post-commit P3APG assertion failure")
    except RuntimeError as error:
        assert "controlled post-commit" in str(error)
    finally:
        _cleanup_p3a_owned_rows(session_factory, created=created)
    _assert_p3a_owned_rows_absent(session_factory, created=created)
