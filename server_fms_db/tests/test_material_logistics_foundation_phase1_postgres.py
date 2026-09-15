"""Guarded PostgreSQL constraints for Phase 1 material logistics foundation."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings
from shared.models.factory import (
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    SupplyMode,
)
from shared.services.physical_ready_service import PhysicalReadyService


pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    if not test_url or make_url(test_url).database != EXPECTED_DATABASE:
        pytest.fail("Phase 1 PostgreSQL tests require guarded smart_factory_benchmark.")
    if settings.database_url.strip() and test_url == settings.database_url.strip():
        pytest.fail("POSTGRES_TEST_DATABASE_URL must not equal DATABASE_URL.")
    return test_url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_DATABASE
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == EXPECTED_REVISION
        labels = connection.execute(
            text("select enumlabel from pg_enum join pg_type on pg_enum.enumtypid = pg_type.oid where typname = :type_name order by enumsortorder"), {"type_name": "supply_mode"}
        ).scalars().all()
        assert labels == ["TRANSPORTED", "MANUAL"]
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def test_group_constraint_null_legacy_compatibility_and_physical_ready_persistence(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"PHASE1PG_{suffix}"
    job_code = f"PHASE1PG_JOB_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Phase 1 PostgreSQL fixture")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=job_code, product_code=product.product_code, status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        structure = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code=f"{job_code}-DEL-1",
            display_name="Structure",
            status=MaterialDeliveryStatus.PENDING,
            supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="TEST_PG_STRUCTURE",
            supply_destination_code="TEST_PG_DEST",
        )
        manual = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=2,
            delivery_code=f"{job_code}-DEL-2",
            display_name="Manual",
            status=MaterialDeliveryStatus.PENDING,
            supply_mode=SupplyMode.MANUAL,
            supply_group_code="TEST_PG_MANUAL",
        )
        session.add_all((structure, manual))
        session.commit()

        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=structure.job_delivery_id,
            request_id="pg-ready-request",
        )
        persisted = session.get(JobMaterialDelivery, structure.job_delivery_id)
        assert persisted is not None
        assert persisted.physical_ready_at is not None
        assert persisted.physical_ready_request_id == "pg-ready-request"

        session.add(
            JobMaterialDelivery(
                production_job_id=job.job_id,
                batch_order=3,
                delivery_code=f"{job_code}-DUPLICATE",
                display_name="Duplicate",
                status=MaterialDeliveryStatus.PENDING,
                supply_mode=SupplyMode.TRANSPORTED,
                supply_group_code="TEST_PG_STRUCTURE",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add_all(
            (
                JobMaterialDelivery(
                    production_job_id=job.job_id,
                    batch_order=4,
                    delivery_code=f"{job_code}-LEGACY-1",
                    display_name="Legacy 1",
                    status=MaterialDeliveryStatus.PENDING,
                ),
                JobMaterialDelivery(
                    production_job_id=job.job_id,
                    batch_order=5,
                    delivery_code=f"{job_code}-LEGACY-2",
                    display_name="Legacy 2",
                    status=MaterialDeliveryStatus.PENDING,
                ),
            )
        )
        session.commit()
        assert session.scalars(
            select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id)
        ).all()

    with session_factory() as session:
        job_id = session.scalar(select(ProductionJob.job_id).where(ProductionJob.job_code == job_code))
        session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job_id))
        session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
        session.execute(delete(Product).where(Product.product_code == product_code))
        session.commit()
