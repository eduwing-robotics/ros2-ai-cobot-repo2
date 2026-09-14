"""Guarded PostgreSQL serialization/lifecycle coverage for the shared DROP resource."""

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

from fms_server.empty_pallet_return_service import EmptyPalletReturnService
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
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
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
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
        pytest.fail("DROP PostgreSQL tests require guarded smart_factory_benchmark.")
    if settings.database_url.strip() and test_url == settings.database_url.strip():
        pytest.fail("POSTGRES_TEST_DATABASE_URL must not equal DATABASE_URL.")
    return test_url


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_size=3, max_overflow=2, pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_DATABASE
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == EXPECTED_REVISION
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def _payload(job_id: int, delivery_id: int, pickup_code: str) -> dict[str, object]:
    return {
        "job_id": job_id,
        "delivery_id": delivery_id,
        "pickup_code": pickup_code,
        "dropoff_code": "DROP",
    }


def _create_eligible_delivery(
    session: Session, *, suffix: str, group: str, pickup_code: str
) -> tuple[int, int, int, str]:
    product_code = f"DROP_PG_PRODUCT_{suffix}"
    product = Product(product_code=product_code, product_name="DROP PostgreSQL fixture")
    part = Part(
        part_code=f"DROP_PG_PART_{suffix}", part_name="DROP part",
        category=PartCategory.STRUCTURE, vision_class=f"drop_class_{suffix}", unit="EA",
    )
    session.add_all((product, part))
    session.flush()
    job = ProductionJob(job_code=f"DROP_PG_JOB_{suffix}", product_code=product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST",
        display_name="Install drop test", part_code=part.part_code, quantity=1,
        supply_mode=SupplyMode.TRANSPORTED, supply_group_code=group,
        supply_destination_code="DROP", status=StepStatus.PENDING,
    )
    session.add(step)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"DROP_PG_DEL_{suffix}",
        display_name="DROP transported delivery", status=MaterialDeliveryStatus.PENDING,
        supply_mode=SupplyMode.TRANSPORTED, supply_group_code=group,
        supply_destination_code="DROP", physical_ready_at=datetime.now(timezone.utc),
        physical_ready_request_id=f"drop-ready-{suffix}",
    )
    session.add(delivery)
    session.flush()
    item = JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id,
        part_code=part.part_code, quantity=1,
    )
    session.add(item)
    session.flush()
    session.add(MaterialInspection(
        inspection_request_id=f"drop-qa-{suffix}", delivery_item_id=item.delivery_item_id,
        inspection_cycle=1, status=MaterialInspectionStatus.COMPLETED,
        result=MaterialInspectionResult.PASS, expected_part_code=part.part_code,
        expected_class_name=part.vision_class, expected_quantity=1, detected_quantity=1,
        production_valid=True,
    ))
    session.commit()
    return job.job_id, delivery.job_delivery_id, step.job_step_id, part.part_code


def _empty_return_service(session: Session) -> EmptyPalletReturnService:
    coordinator = ForkliftExecutionCoordinator(
        session, adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    return EmptyPalletReturnService(session, forklift_execution_coordinator=coordinator)


def _cleanup(
    session_factory: sessionmaker[Session], *, product_codes: list[str], job_ids: list[int]
) -> None:
    with session_factory() as session:
        delivery_ids = list(session.scalars(select(JobMaterialDelivery.job_delivery_id).where(
            JobMaterialDelivery.production_job_id.in_(job_ids)
        )))
        item_ids = list(session.scalars(select(JobMaterialDeliveryItem.delivery_item_id).where(
            JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
        )))
        step_ids = list(session.scalars(select(JobStep.job_step_id).where(JobStep.job_id.in_(job_ids))))
        part_codes = list(session.scalars(select(JobStep.part_code).where(JobStep.job_step_id.in_(step_ids))))
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id.in_(delivery_ids)))
        session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(item_ids)))
        session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id.in_(item_ids)))
        session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(delivery_ids)))
        session.execute(delete(JobStep).where(JobStep.job_step_id.in_(step_ids)))
        session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
        session.execute(delete(Part).where(Part.part_code.in_(part_codes)))
        session.execute(delete(Product).where(Product.product_code.in_(product_codes)))
        session.commit()


def test_postgres_advisory_guard_allows_only_one_outer_inner_material_claim(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    with session_factory() as session:
        outer_job, outer_delivery, _, _ = _create_eligible_delivery(
            session, suffix=f"{suffix}_OUTER", group="OUTER_WALLS", pickup_code="RACK1"
        )
        inner_job, inner_delivery, _, _ = _create_eligible_delivery(
            session, suffix=f"{suffix}_INNER", group="INNER_WALL", pickup_code="RACK2"
        )

    barrier = threading.Barrier(2)

    def claim_once(job_id: int, delivery_id: int, pickup_code: str) -> tuple[bool, TransportEligibilityReason]:
        with session_factory() as session:
            barrier.wait(timeout=10)
            result = TransportEligibilityService(session).claim_transport(
                job_delivery_id=delivery_id, request_payload=_payload(job_id, delivery_id, pickup_code)
            )
            return result.eligible, result.reason

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda args: claim_once(*args),
                [(outer_job, outer_delivery, "RACK1"), (inner_job, inner_delivery, "RACK2")],
            ))
        assert sum(eligible for eligible, _ in results) == 1
        assert sorted(reason.value for _, reason in results) == [
            TransportEligibilityReason.DROP_RESOURCE_OCCUPIED.value,
            TransportEligibilityReason.ELIGIBLE.value,
        ]

        with session_factory() as session:
            attempts = list(session.scalars(select(ExecutionAttempt).where(
                ExecutionAttempt.job_delivery_id.in_((outer_delivery, inner_delivery)),
                ExecutionAttempt.command_type == "EXECUTE_TRANSPORT",
            )))
            statuses = [session.get(JobMaterialDelivery, delivery_id).status for delivery_id in (outer_delivery, inner_delivery)]
            assert len(attempts) == 1
            assert statuses.count(MaterialDeliveryStatus.IN_PROGRESS) == 1
            assert statuses.count(MaterialDeliveryStatus.PENDING) == 1
    finally:
        _cleanup(
            session_factory,
            product_codes=[f"DROP_PG_PRODUCT_{suffix}_OUTER", f"DROP_PG_PRODUCT_{suffix}_INNER"],
            job_ids=[outer_job, inner_job],
        )


def test_empty_return_success_releases_drop_but_delivery_remains_completed(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    with session_factory() as session:
        outer_job, outer_delivery, _, _ = _create_eligible_delivery(
            session, suffix=f"{suffix}_OUTER", group="OUTER_WALLS", pickup_code="RACK1"
        )
        inner_job, inner_delivery, _, _ = _create_eligible_delivery(
            session, suffix=f"{suffix}_INNER", group="INNER_WALL", pickup_code="RACK2"
        )

    try:
        with session_factory() as session:
            first = TransportEligibilityService(session).claim_transport(
                job_delivery_id=outer_delivery, request_payload=_payload(outer_job, outer_delivery, "RACK1")
            )
            assert first.eligible is True and first.request_id is not None
            ExecutionAttemptService(session).apply_result(
                first.request_id, ExecutionAttemptStatus.SUCCEEDED, result_payload={"status": "SUCCEEDED"}
            )
            session.commit()
            MaterialDeliveryService(session).complete_delivery(outer_delivery)
            blocked = TransportEligibilityService(session).claim_transport(
                job_delivery_id=inner_delivery, request_payload=_payload(inner_job, inner_delivery, "RACK2")
            )
            assert blocked.reason is TransportEligibilityReason.DROP_RESOURCE_OCCUPIED
            assert session.get(JobMaterialDelivery, outer_delivery).status is MaterialDeliveryStatus.COMPLETED

            returned = _empty_return_service(session).execute_empty_pallet_return(
                job_delivery_id=outer_delivery
            )
            assert returned.status.value == "SUCCEEDED"
            assert session.get(JobMaterialDelivery, outer_delivery).status is MaterialDeliveryStatus.COMPLETED

            released = TransportEligibilityService(session).claim_transport(
                job_delivery_id=inner_delivery, request_payload=_payload(inner_job, inner_delivery, "RACK2")
            )
            assert released.eligible is True
    finally:
        _cleanup(
            session_factory,
            product_codes=[f"DROP_PG_PRODUCT_{suffix}_OUTER", f"DROP_PG_PRODUCT_{suffix}_INNER"],
            job_ids=[outer_job, inner_job],
        )
