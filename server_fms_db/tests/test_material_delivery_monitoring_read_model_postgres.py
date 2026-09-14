"""Guarded PostgreSQL read-model coverage for latest Incoming QA cycle selection."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings
from shared.models.factory import (
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
from shared.services.material_delivery_monitoring_service import MaterialDeliveryMonitoringService

pytestmark = pytest.mark.postgres_integration
_EXPECTED_DB = "smart_factory_benchmark"
_EXPECTED_REVISION = "20260904_02"


def _test_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run guarded PostgreSQL integration.")
    settings = get_settings()
    url = settings.postgres_test_database_url.strip()
    if not url or make_url(url).database != _EXPECTED_DB or url == settings.database_url.strip():
        pytest.fail("Guarded smart_factory_benchmark target required.")
    return url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_test_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == _EXPECTED_DB
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == _EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def test_monitoring_uses_latest_cycle_and_group_release_on_postgres(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"QAMON_PG_{suffix}"
    part_code = f"QAMON_PART_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="QA monitor PG fixture")
        part = Part(part_code=part_code, part_name="QA monitor part", category=PartCategory.STRUCTURE, unit="EA", vision_class="qa_monitor_pg")
        session.add_all((product, part))
        session.flush()
        job = ProductionJob(job_code=f"QAMON_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST", display_name="QA monitor", part_code=part.part_code, quantity=1, vision_class=part.vision_class, supply_mode=SupplyMode.TRANSPORTED, supply_group_code="QAMON_GROUP", status=StepStatus.PENDING)
        delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code=f"QAMON_DEL_{suffix}", display_name="QA monitor", status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED, supply_group_code="QAMON_GROUP")
        session.add_all((step, delivery))
        session.flush()
        item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=part.part_code, quantity=1)
        session.add(item)
        session.flush()
        session.add_all((
            MaterialInspection(inspection_request_id=f"qamon-pg-fail-{suffix}", delivery_item_id=item.delivery_item_id, inspection_cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.FAIL, expected_part_code=part.part_code, expected_class_name=part.vision_class, expected_quantity=1, production_valid=False),
            MaterialInspection(inspection_request_id=f"qamon-pg-pass-{suffix}", delivery_item_id=item.delivery_item_id, inspection_cycle=2, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, expected_part_code=part.part_code, expected_class_name=part.vision_class, expected_quantity=1, production_valid=True),
        ))
        session.commit()
        job_id, delivery_id, item_id, step_id = job.job_id, delivery.job_delivery_id, item.delivery_item_id, step.job_step_id

    try:
        with session_factory() as session:
            delivery_view = MaterialDeliveryMonitoringService(session).get_deliveries_for_job(job_id=job_id)
            assert len(delivery_view) == 1
            assert delivery_view[0].qa_total_items == 1
            assert delivery_view[0].qa_released_items == 1
            assert delivery_view[0].qa_all_released is True
            assert delivery_view[0].items[0].qa_state == "RELEASED"
            assert delivery_view[0].items[0].latest_inspection is not None
            assert delivery_view[0].items[0].latest_inspection.inspection_cycle == 2
    finally:
        with session_factory() as session:
            session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id == item_id))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id == item_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == delivery_id))
            session.execute(delete(JobStep).where(JobStep.job_step_id == step_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
            session.execute(delete(Part).where(Part.part_code == part_code))
            session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()
