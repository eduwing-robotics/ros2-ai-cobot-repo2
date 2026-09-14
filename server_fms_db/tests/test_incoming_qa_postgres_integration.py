"""Guarded PostgreSQL coverage for the persisted Incoming QA v0.1 runtime."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.incoming_material_qa_http_client import IncomingMaterialQAHttpClient
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.config import Settings, get_settings
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialInspection,
    MaterialInspectionStatus,
    MaterialDeliveryStatus,
    Part,
    Product,
    ProductionJob,
    StepStatus,
)
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.material_inspection_service import MaterialInspectionService

pytestmark = pytest.mark.postgres_integration
_EXPECTED_DB = "smart_factory_benchmark"
_EXPECTED_REVISION = "20260904_02"


def _test_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run guarded Incoming QA PostgreSQL integration.")
    settings = get_settings()
    url = settings.postgres_test_database_url.strip()
    if not url or make_url(url).database != _EXPECTED_DB or url == settings.database_url.strip():
        pytest.fail("Refusing Incoming QA PostgreSQL test: guarded smart_factory_benchmark target required.")
    return url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_test_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == _EXPECTED_DB
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == _EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def qa_item(session_factory: sessionmaker[Session]) -> Iterator[int]:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"QA_PG_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Incoming QA PG fixture")
        part = Part(part_code=f"QA_PART_{suffix}", part_name="Incoming QA part", category="STRUCTURE", unit="EA", vision_class="wall_ext_back")
        job = ProductionJob(job_code=f"QA_JOB_{suffix}", product_code=product_code, status=JobStatus.REQUESTED)
        session.add_all([product, part, job])
        session.flush()
        step = JobStep(job_id=job.job_id, step_order=1, operation_code="QA_PG", display_name="Incoming QA", status=StepStatus.PENDING)
        delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code=f"QA_DELIVERY_{suffix}", display_name="Incoming QA", status=MaterialDeliveryStatus.PENDING)
        session.add_all([step, delivery])
        session.flush()
        item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=part.part_code, quantity=2)
        session.add(item)
        session.commit()
        item_id, job_id, delivery_id, step_id, part_code = item.delivery_item_id, job.job_id, delivery.job_delivery_id, step.job_step_id, part.part_code
    try:
        yield item_id
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


def test_persisted_snapshot_and_callback_hold_on_guarded_postgres(session_factory: sessionmaker[Session], qa_item: int) -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        sent.append(json.loads(request.content))
        return httpx.Response(202, request=request)

    settings = Settings(vision_incoming_qa_base_url="http://vision.test", vision_incoming_qa_request_path="/api/v1/incoming-qa/requests", vision_incoming_qa_timeout_seconds=0.1, vision_incoming_qa_max_attempts=1)
    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=settings, client=httpx.Client(transport=httpx.MockTransport(handler))))
    with session_factory() as session:
        runtime.create_and_send(session, delivery_item_id=qa_item)
        inspection = session.scalar(select(MaterialInspection).where(MaterialInspection.delivery_item_id == qa_item))
        assert inspection is not None
        assert inspection.status == MaterialInspectionStatus.RUNNING
        assert sent[0]["inspection_request_id"] == inspection.inspection_request_id
        result = IncomingMaterialQAResult(ver="0.1", inspection_request_id=inspection.inspection_request_id, delivery_item_id=qa_item, inspection_cycle=inspection.inspection_cycle, status="COMPLETED", result="PASS", expected_part_code=inspection.expected_part_code, expected_class_name=inspection.expected_class_name, expected_quantity=2, detected_quantity=2, detections=[], frame_width=640, frame_height=480, camera_source="GLOBAL_CAMERA", frame_seq=1, timestamp=datetime.now(timezone.utc), model_scope="VISION", model_version="v0.1", production_valid=False)
        applied = MaterialInspectionService().apply_inspection_result(session, result)
        session.commit()
        assert applied.status == MaterialInspectionStatus.COMPLETED
        assert MaterialInspectionService().is_delivery_item_released(session, qa_item) is False
