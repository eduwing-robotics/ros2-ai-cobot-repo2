"""Guarded PostgreSQL concurrency coverage for Incoming QA preparation."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.incoming_material_qa_dispatch_coordinator import (
    IncomingMaterialQADispatchAction,
    IncomingMaterialQADispatchCoordinator,
)
from shared.config import get_settings
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)

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
    engine: Engine = create_engine(_test_url(), pool_size=2, max_overflow=2, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == _EXPECTED_DB
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == _EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def test_two_sessions_prepare_one_incoming_qa_cycle(session_factory: sessionmaker[Session]) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"QADISP_PG_{suffix}"
    part_code = f"QADISP_PART_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Incoming QA dispatcher PG fixture")
        part = Part(part_code=part_code, part_name="Incoming QA dispatcher part", category=PartCategory.STRUCTURE, unit="EA", vision_class="qa_dispatch_pg")
        session.add_all((product, part))
        session.flush()
        job = ProductionJob(job_code=f"QADISP_JOB_{suffix}", product_code=product.product_code, status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST", display_name="Incoming QA", part_code=part.part_code, quantity=1, supply_mode=SupplyMode.TRANSPORTED, supply_group_code="QADISP_GROUP", status=StepStatus.PENDING)
        delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code=f"QADISP_DEL_{suffix}", display_name="Incoming QA", status=MaterialDeliveryStatus.PENDING, supply_mode=SupplyMode.TRANSPORTED, supply_group_code="QADISP_GROUP")
        session.add_all((step, delivery))
        session.flush()
        item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=part.part_code, quantity=1)
        session.add(item)
        session.commit()
        job_id, step_id, delivery_id, item_id = job.job_id, step.job_step_id, delivery.job_delivery_id, item.delivery_item_id

    barrier = threading.Barrier(2)

    def prepare_once():
        with session_factory() as session:
            barrier.wait(timeout=10)
            return IncomingMaterialQADispatchCoordinator(session).prepare_inspection(delivery_item_id=item_id)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: prepare_once(), range(2)))
        assert sorted(result.action for result in results) == [
            IncomingMaterialQADispatchAction.CREATE_NEW,
            IncomingMaterialQADispatchAction.REUSE_EXISTING,
        ]
        assert {result.inspection_cycle for result in results} == {1}
        assert len({result.inspection_request_id for result in results}) == 1
        with session_factory() as session:
            inspections = list(session.scalars(select(MaterialInspection).where(MaterialInspection.delivery_item_id == item_id)))
            assert len(inspections) == 1 and inspections[0].inspection_cycle == 1
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



def test_two_sessions_globally_serialize_distinct_incoming_qa_items(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    records: list[tuple[int, int, int, int, str, str]] = []
    with session_factory() as session:
        for label in ("A", "B"):
            product = Product(
                product_code=f"QAGLOBAL_PG_{suffix}_{label}",
                product_name="Incoming QA global fixture",
            )
            part = Part(
                part_code=f"QAGLOBAL_PART_{suffix}_{label}",
                part_name="Incoming QA global part",
                category=PartCategory.STRUCTURE,
                unit="EA",
                vision_class=f"qa_global_{label.lower()}",
            )
            session.add_all((product, part))
            session.flush()
            job = ProductionJob(
                job_code=f"QAGLOBAL_JOB_{suffix}_{label}",
                product_code=product.product_code,
                status=JobStatus.REQUESTED,
            )
            session.add(job)
            session.flush()
            step = JobStep(
                job_id=job.job_id,
                step_order=1,
                operation_code="INSTALL_TEST",
                display_name="Incoming QA global",
                part_code=part.part_code,
                quantity=1,
                supply_mode=SupplyMode.TRANSPORTED,
                supply_group_code="QAGLOBAL_GROUP",
                status=StepStatus.PENDING,
            )
            delivery = JobMaterialDelivery(
                production_job_id=job.job_id,
                batch_order=1,
                delivery_code=f"QAGLOBAL_DEL_{suffix}_{label}",
                display_name="Incoming QA global",
                status=MaterialDeliveryStatus.PENDING,
                supply_mode=SupplyMode.TRANSPORTED,
                supply_group_code="QAGLOBAL_GROUP",
            )
            session.add_all((step, delivery))
            session.flush()
            item = JobMaterialDeliveryItem(
                job_delivery_id=delivery.job_delivery_id,
                job_step_id=step.job_step_id,
                part_code=part.part_code,
                quantity=1,
            )
            session.add(item)
            session.flush()
            records.append((job.job_id, step.job_step_id, delivery.job_delivery_id, item.delivery_item_id, part.part_code, product.product_code))
        session.commit()

    barrier = threading.Barrier(2)

    def prepare(item_id: int):
        with session_factory() as session:
            barrier.wait(timeout=10)
            return IncomingMaterialQADispatchCoordinator(session).prepare_inspection(
                delivery_item_id=item_id
            )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(prepare, [record[3] for record in records]))
        assert {result.action for result in results} == {
            IncomingMaterialQADispatchAction.CREATE_NEW,
            IncomingMaterialQADispatchAction.GLOBAL_BUSY,
        }
        with session_factory() as session:
            active = list(session.scalars(select(MaterialInspection).where(
                MaterialInspection.delivery_item_id.in_([record[3] for record in records])
            )))
            assert len(active) == 1
            assert active[0].status.value in {"REQUESTED", "RUNNING"}
    finally:
        with session_factory() as session:
            item_ids = [record[3] for record in records]
            delivery_ids = [record[2] for record in records]
            step_ids = [record[1] for record in records]
            job_ids = [record[0] for record in records]
            part_codes = [record[4] for record in records]
            product_codes = [record[5] for record in records]
            session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(item_ids)))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id.in_(item_ids)))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(delivery_ids)))
            session.execute(delete(JobStep).where(JobStep.job_step_id.in_(step_ids)))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
            session.execute(delete(Part).where(Part.part_code.in_(part_codes)))
            session.execute(delete(Product).where(Product.product_code.in_(product_codes)))
            session.commit()
