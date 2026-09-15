"""Guarded PostgreSQL race coverage for v0.2 per-item-cycle orchestration."""

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

from fms_server.incoming_qa_v02_orchestration_service import (
    IncomingQAV02ActiveInspectionError,
    IncomingQAV02OrchestrationService,
)
from shared.config import get_settings
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
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
    engine: Engine = create_engine(_test_url(), pool_size=3, max_overflow=2, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT current_database()")).scalar_one() == _EXPECTED_DB
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def test_two_sessions_allocate_one_base_reinspection_cycle(session_factory: sessionmaker[Session]) -> None:
    suffix = uuid.uuid4().hex[:10].upper()
    rows = (
        ("base_house_b", f"QA2B_BASE_{suffix}"),
        ("wall_ext_back_window", f"QA2B_B01_{suffix}"),
        ("wall_ext_door", f"QA2B_B02_{suffix}"),
        ("wall_ext_left_window", f"QA2B_B03_{suffix}"),
        ("wall_ext_right", f"QA2B_B04_{suffix}"),
        ("wall_int_house_b", f"QA2B_B05_{suffix}"),
        ("roof_zip", f"QA2B_B06_{suffix}"),
    )
    ids: dict[str, object] = {}
    created_product = False
    with session_factory() as session:
        product = session.get(Product, "HOUSE_B")
        if product is None:
            product = Product(product_code="HOUSE_B", product_name="House B")
            session.add(product)
            session.flush()
            created_product = True
        job = ProductionJob(job_code=f"QA2B_JOB_{suffix}", product_code="HOUSE_B", status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code=f"QA2B_DEL_{suffix}", display_name="QA2B", status="PENDING")
        session.add(delivery)
        session.flush()
        session.add_all([Part(part_code=code, part_name=code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA") for vision_class, code in rows])
        session.flush()
        session.add_all([JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=code, quantity=1) for _vision_class, code in rows])
        session.commit()
        planned = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job.job_id)
        base_transaction = session.get(IncomingQATransaction, planned.send_transaction_id)
        assert base_transaction is not None
        # Persist valid terminal base evidence without a network transport; the
        # race below targets allocation, not Phase 2A packet handling.
        base_transaction.status = IncomingQATransactionStatus.COMPLETED
        base_transaction.overall_result = MaterialInspectionResult.PASS
        base_transaction.production_valid = True
        base_inspection = session.scalar(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == base_transaction.transaction_id
        ))
        assert base_inspection is not None
        base_inspection.status = MaterialInspectionStatus.COMPLETED
        base_inspection.result = MaterialInspectionResult.PASS
        base_inspection.production_valid = True
        session.commit()
        ids.update(job=job.job_id, delivery=delivery.job_delivery_id, base_item=base_inspection.delivery_item_id,
                   transaction=base_transaction.transaction_id, parts=[code for _vision, code in rows])

    barrier = threading.Barrier(2)

    def allocate() -> str:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                result = IncomingQAV02OrchestrationService(session).request_reinspection(
                    job_id=int(ids["job"]), delivery_item_ids=[int(ids["base_item"])]
                )
                return f"created:{result.created_transaction_ids}"
            except IncomingQAV02ActiveInspectionError:
                return "active"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _ignored: allocate(), range(2)))
        assert len([outcome for outcome in outcomes if outcome.startswith("created:")]) == 1
        assert outcomes.count("active") == 1
        with session_factory() as session:
            inspections = list(session.scalars(select(MaterialInspection).where(
                MaterialInspection.delivery_item_id == ids["base_item"]
            ).order_by(MaterialInspection.inspection_cycle)))
            assert [inspection.inspection_cycle for inspection in inspections] == [1, 2]
            cycle2 = [inspection for inspection in inspections if inspection.inspection_cycle == 2]
            assert len(cycle2) == 1
    finally:
        with session_factory() as session:
            tx_ids = list(session.scalars(select(IncomingQATransaction.transaction_id).where(
                IncomingQATransaction.production_job_id == ids["job"]
            )))
            if tx_ids:
                session.execute(delete(MaterialInspection).where(MaterialInspection.incoming_qa_transaction_id.in_(tx_ids)))
                session.execute(delete(IncomingQATransaction).where(IncomingQATransaction.transaction_id.in_(tx_ids)))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id == ids["delivery"]))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == ids["delivery"]))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == ids["job"]))
            session.execute(delete(Part).where(Part.part_code.in_(ids["parts"])))
            if created_product:
                session.execute(delete(Product).where(Product.product_code == "HOUSE_B"))
            session.commit()
