"""Guarded PostgreSQL concurrency coverage for Phase 2 transport claims."""

from __future__ import annotations

import concurrent.futures
import os
import threading
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
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
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
from shared.services.transport_eligibility_service import (
    TransportEligibilityReason,
    TransportEligibilityService,
)


pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    if not test_url or make_url(test_url).database != EXPECTED_DATABASE:
        pytest.fail("Phase 2 PostgreSQL tests require guarded smart_factory_benchmark.")
    if settings.database_url.strip() and test_url == settings.database_url.strip():
        pytest.fail("POSTGRES_TEST_DATABASE_URL must not equal DATABASE_URL.")
    return test_url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_size=2, max_overflow=2, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_DATABASE
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def _payload(job_id: int, delivery_id: int) -> dict:
    return {
        "job_id": job_id,
        "delivery_id": delivery_id,
        "pickup_code": "RACK1",
        "dropoff_code": "DROP",
    }


def test_two_sessions_claim_one_policy_transport_delivery(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"P2PG_{suffix}"
    job_code = f"P2PG_JOB_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Phase 2 concurrency fixture")
        part = Part(
            part_code=f"P2PG_PART_{suffix}",
            part_name="Phase 2 part",
            category=PartCategory.STRUCTURE,
            vision_class="phase2_test_class",
            unit="EA",
        )
        session.add_all((product, part))
        session.flush()
        job = ProductionJob(job_code=job_code, product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        step = JobStep(
            job_id=job.job_id,
            step_order=1,
            operation_code="INSTALL_TEST",
            display_name="Install test",
            part_code=part.part_code,
            quantity=1,
            supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="TEST_P2_GROUP",
            status=StepStatus.PENDING,
        )
        session.add(step)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code=f"{job_code}-DEL-1",
            display_name="Phase 2 transported Delivery",
            status=MaterialDeliveryStatus.PENDING,
            supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="TEST_P2_GROUP",
            physical_ready_at=datetime.now(timezone.utc),
            physical_ready_request_id="phase2-pg-ready",
        )
        session.add(delivery)
        session.flush()
        item = JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id,
            job_step_id=step.job_step_id,
            part_code=part.part_code,
            quantity=1,
        )
        session.add(item)
        session.flush()
        session.add(
            MaterialInspection(
                inspection_request_id=f"phase2-pg-qa-{suffix}",
                delivery_item_id=item.delivery_item_id,
                inspection_cycle=1,
                status=MaterialInspectionStatus.COMPLETED,
                result=MaterialInspectionResult.PASS,
                expected_part_code=part.part_code,
                expected_class_name=part.vision_class,
                expected_quantity=1,
                detected_quantity=1,
                production_valid=True,
            )
        )
        session.commit()
        job_id = job.job_id
        delivery_id = delivery.job_delivery_id
        item_id = item.delivery_item_id
        step_id = step.job_step_id

    barrier = threading.Barrier(2)

    def claim_once() -> tuple[bool, TransportEligibilityReason]:
        with session_factory() as session:
            barrier.wait(timeout=10)
            result = TransportEligibilityService(session).claim_transport(
                job_delivery_id=delivery_id,
                request_payload=_payload(job_id, delivery_id),
            )
            return result.eligible, result.reason

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim_once(), range(2)))
        assert sum(eligible for eligible, _ in results) == 1
        assert sorted(reason for _, reason in results) == [
            TransportEligibilityReason.DELIVERY_NOT_PENDING,
            TransportEligibilityReason.ELIGIBLE,
        ]
        with session_factory() as session:
            delivery = session.get(JobMaterialDelivery, delivery_id)
            attempts = list(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery_id)))
            assert delivery is not None and delivery.status is MaterialDeliveryStatus.IN_PROGRESS
            assert len(attempts) == 1
    finally:
        with session_factory() as session:
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery_id))
            session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id == item_id))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id == item_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == delivery_id))
            session.execute(delete(JobStep).where(JobStep.job_step_id == step_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
            session.execute(delete(Part).where(Part.part_code == f"P2PG_PART_{suffix}"))
            session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()
