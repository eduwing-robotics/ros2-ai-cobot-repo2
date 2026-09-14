"""Guarded PostgreSQL serialization coverage for manual transport recovery."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.manual_transport_recovery_service import ManualTransportRecoveryService
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
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
from shared.services.drop_resource_service import DropResourceService
from shared.services.transport_eligibility_service import TransportEligibilityService

pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    benchmark = settings.postgres_test_database_url.strip()
    production = settings.database_url.strip()
    if not benchmark or make_url(benchmark).database != EXPECTED_DATABASE:
        pytest.fail("Manual recovery PostgreSQL test requires smart_factory_benchmark.")
    if not production or benchmark == production:
        pytest.fail("POSTGRES_TEST_DATABASE_URL must be non-empty and distinct from DATABASE_URL.")
    return benchmark


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_size=4, max_overflow=2, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == EXPECTED_DATABASE
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def _delivery(session: Session, *, suffix: str, group: str, status: MaterialDeliveryStatus, physical_ready: bool) -> tuple[JobMaterialDelivery, Part]:
    product_code = f"RECOVERY_PG_PRODUCT_{suffix}"
    product = Product(product_code=product_code, product_name="Recovery PG product")
    part = Part(part_code=f"RECOVERY_PG_PART_{suffix}", part_name="Recovery part", category=PartCategory.STRUCTURE, vision_class="recovery_class", unit="EA")
    session.add_all((product, part))
    session.flush()
    job = ProductionJob(job_code=f"RECOVERY_PG_JOB_{suffix}", product_code=product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST", display_name="Recovery test", part_code=part.part_code, quantity=1, supply_mode=SupplyMode.TRANSPORTED, supply_group_code=group, supply_destination_code="DROP", status=StepStatus.PENDING)
    session.add(step)
    session.flush()
    delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code=f"RECOVERY_PG_DEL_{suffix}", display_name="Recovery PG delivery", status=status, supply_mode=SupplyMode.TRANSPORTED, supply_group_code=group, supply_destination_code="DROP", physical_ready_at=(__import__("datetime").datetime.now(__import__("datetime").timezone.utc) if physical_ready else None))
    session.add(delivery)
    session.flush()
    item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=part.part_code, quantity=1)
    session.add(item)
    session.flush()
    if physical_ready:
        session.add(MaterialInspection(inspection_request_id=f"recovery-pg-qa-{suffix}", delivery_item_id=item.delivery_item_id, inspection_cycle=1, status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS, expected_part_code=part.part_code, expected_class_name=part.vision_class, expected_quantity=1, detected_quantity=1, production_valid=True))
    session.flush()
    return delivery, part


def _cleanup(session: Session, product_codes: list[str]) -> None:
    job_ids = list(session.scalars(select(ProductionJob.job_id).where(ProductionJob.product_code.in_(product_codes))))
    delivery_ids = list(session.scalars(select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id.in_(job_ids))))
    item_ids = list(session.scalars(select(JobMaterialDeliveryItem.delivery_item_id).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids))))
    session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(item_ids)))
    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id.in_(delivery_ids)))
    session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)))
    session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(delivery_ids)))
    session.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids)))
    session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
    session.execute(delete(Part).where(Part.part_code.in_([f"RECOVERY_PG_PART_{code.split('_')[-1]}" for code in product_codes])))
    session.execute(delete(Product).where(Product.product_code.in_(product_codes)))
    session.commit()


def test_recovery_at_source_and_other_delivery_claim_are_serialized_by_postgres_drop_guard(session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    suffix_a = uuid.uuid4().hex[:12]
    suffix_b = uuid.uuid4().hex[:12]
    product_a = f"RECOVERY_PG_PRODUCT_{suffix_a}"
    product_b = f"RECOVERY_PG_PRODUCT_{suffix_b}"
    with session_factory() as session:
        delivery_a, _ = _delivery(session, suffix=suffix_a, group="OUTER_WALLS", status=MaterialDeliveryStatus.IN_PROGRESS, physical_ready=False)
        delivery_b, _ = _delivery(session, suffix=suffix_b, group="INNER_WALL", status=MaterialDeliveryStatus.PENDING, physical_ready=True)
        attempt_a = ExecutionAttempt(req_id=f"recovery-pg-attempt-{suffix_a}", executor_type=ExecutorType.FORKLIFT, command_type="EXECUTE_TRANSPORT", job_id=delivery_a.production_job_id, job_delivery_id=delivery_a.job_delivery_id, attempt_no=1, status=ExecutionAttemptStatus.CREATED, request_payload_json=json.dumps({"job_id":delivery_a.production_job_id,"delivery_id":delivery_a.job_delivery_id,"pickup_code":"RACK1","dropoff_code":"DROP"}))
        session.add(attempt_a)
        session.commit()
        delivery_a_id, job_a_id, attempt_a_id = delivery_a.job_delivery_id, delivery_a.production_job_id, attempt_a.attempt_id
        delivery_b_id, job_b_id = delivery_b.job_delivery_id, delivery_b.production_job_id

    acquired = threading.Event()
    release = threading.Event()
    claim_finished = threading.Event()
    original = DropResourceService.acquire_drop_transaction_guard

    def guarded(self):
        original(self)
        if threading.current_thread().name == "recovery-thread":
            acquired.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(DropResourceService, "acquire_drop_transaction_guard", guarded)
    outcomes: dict[str, object] = {}

    def recover() -> None:
        with session_factory() as session:
            outcomes["recovery"] = ManualTransportRecoveryService(session).confirm_location(
                production_job_id=job_a_id, job_delivery_id=delivery_a_id, attempt_id=attempt_a_id,
                confirmed_location_code="RACK1",
            )

    def claim() -> None:
        with session_factory() as session:
            outcomes["claim"] = TransportEligibilityService(session).claim_transport(
                job_delivery_id=delivery_b_id,
                request_payload={"job_id": job_b_id, "delivery_id": delivery_b_id, "pickup_code": "RACK2", "dropoff_code": "DROP"},
            )
            claim_finished.set()

    recovery_thread = threading.Thread(name="recovery-thread", target=recover)
    claim_thread = threading.Thread(name="claim-thread", target=claim)
    recovery_thread.start()
    assert acquired.wait(timeout=5)
    claim_thread.start()
    assert not claim_finished.wait(timeout=0.25)
    release.set()
    recovery_thread.join(timeout=5)
    claim_thread.join(timeout=5)

    try:
        assert not recovery_thread.is_alive() and not claim_thread.is_alive()
        assert outcomes["recovery"].derived_drop_state.value == "FREE"
        assert outcomes["claim"].eligible is True
        with session_factory() as session:
            assert session.get(JobMaterialDelivery, delivery_a_id).status is MaterialDeliveryStatus.PENDING
            assert session.get(JobMaterialDelivery, delivery_b_id).status is MaterialDeliveryStatus.IN_PROGRESS
            assert session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery_b_id)) is not None
    finally:
        with session_factory() as session:
            _cleanup(session, [product_a, product_b])
