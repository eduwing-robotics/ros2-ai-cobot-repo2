"""Guarded PostgreSQL atomic-apply coverage for Incoming QA v0.2 UDP runtime."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.incoming_material_qa_udp_transport import (
    IncomingQAUdpRuntime,
    IncomingQAUdpRuntimeConfig,
)
from shared.config import get_settings
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import (
    IncomingQARequestItemV02,
    IncomingQARequestV02,
    IncomingQAResultItemV02,
    IncomingQAResultV02,
)
from shared.services.incoming_qa_transaction_service import IncomingQATransactionService
from shared.vision_recipe_mapping import IncomingQAInspectionMode

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
        assert connection.execute(text("SELECT current_database()")).scalar_one() == _EXPECTED_DB
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def test_v02_final_result_is_atomic_on_guarded_postgres(session_factory: sessionmaker[Session]) -> None:
    suffix = uuid.uuid4().hex[:10].upper()
    ids: dict[str, int | str] = {}
    with session_factory() as session:
        product = Product(product_code=f"UDP_PG_{suffix}", product_name="UDP PG")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=f"UDP_PG_JOB_{suffix}", product_code=product.product_code, status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id, batch_order=1, delivery_code=f"UDP_PG_DEL_{suffix}",
            display_name="UDP PG", status="PENDING",
        )
        session.add(delivery)
        session.flush()
        parts = [
            Part(part_code=f"UDP_PG_B01_{suffix}", part_name="B01", category=PartCategory.STRUCTURE, unit="EA", vision_class="wall_ext_back_window"),
            Part(part_code=f"UDP_PG_B02_{suffix}", part_name="B02", category=PartCategory.STRUCTURE, unit="EA", vision_class="wall_ext_door"),
        ]
        session.add_all(parts)
        session.flush()
        session.add_all([
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=parts[0].part_code, quantity=1),
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=parts[1].part_code, quantity=1),
        ])
        session.flush()
        items = list(session.scalars(select(JobMaterialDeliveryItem).where(
            JobMaterialDeliveryItem.job_delivery_id == delivery.job_delivery_id
        ).order_by(JobMaterialDeliveryItem.delivery_item_id)))
        request = IncomingQARequestV02(
            inspection_request_id=f"REQ_UDP_PG_{suffix}", inspection_cycle=1,
            inspection_mode=IncomingQAInspectionMode.HOUSE_B,
            items=[
                IncomingQARequestItemV02(slot_id="B01", delivery_item_id=items[0].delivery_item_id, expected_part_code=parts[0].part_code, expected_class_name="wall_ext_back_window", expected_quantity=1),
                IncomingQARequestItemV02(slot_id="B02", delivery_item_id=items[1].delivery_item_id, expected_part_code=parts[1].part_code, expected_class_name="wall_ext_door", expected_quantity=1),
            ],
        )
        transaction = IncomingQATransactionService().create_or_get(
            session, production_job_id=job.job_id, request=request
        ).transaction
        session.commit()
        ids.update(job=job.job_id, delivery=delivery.job_delivery_id, transaction=transaction.transaction_id,
                   product=product.product_code, part_a=parts[0].part_code, part_b=parts[1].part_code)

    runtime = IncomingQAUdpRuntime(
        session_factory=session_factory,
        config=IncomingQAUdpRuntimeConfig(
            vision_host="127.0.0.1", vision_port=9, result_host="127.0.0.1", result_port=0,
            ack_timeout_seconds=1, max_retries=0,
        ),
    )
    result = IncomingQAResultV02(
        inspection_request_id=request.inspection_request_id, inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.HOUSE_B, result="PASS", production_valid=True,
        items=[
            IncomingQAResultItemV02(**item.model_dump(), predicted_class_name=item.expected_class_name,
                                    material_confidence=0.99, detected_quantity=1, result="PASS")
            for item in request.items
        ],
        camera_source="GLOBAL_CAMERA", timestamp="2026-09-04T02:00:00Z",
        model_scope="udp-pg", model_version="v0.2-test",
    )
    try:
        assert asyncio.run(runtime.handle_result(result))
        # Exact terminal replay is an idempotent no-op against actual PostgreSQL.
        assert asyncio.run(runtime.handle_result(result))
        with session_factory() as session:
            transaction = session.get(IncomingQATransaction, ids["transaction"])
            assert transaction is not None and transaction.status is IncomingQATransactionStatus.COMPLETED
            inspections = list(session.scalars(select(MaterialInspection).where(
                MaterialInspection.incoming_qa_transaction_id == ids["transaction"]
            )))
            assert len(inspections) == 2
            assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.COMPLETED}
    finally:
        with session_factory() as session:
            session.execute(delete(MaterialInspection).where(MaterialInspection.incoming_qa_transaction_id == ids["transaction"]))
            session.execute(delete(IncomingQATransaction).where(IncomingQATransaction.transaction_id == ids["transaction"]))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id == ids["delivery"]))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == ids["delivery"]))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == ids["job"]))
            session.execute(delete(Part).where(Part.part_code.in_([ids["part_a"], ids["part_b"]])))
            session.execute(delete(Product).where(Product.product_code == ids["product"]))
            session.commit()
