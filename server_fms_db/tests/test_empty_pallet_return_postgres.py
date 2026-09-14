"""Guarded PostgreSQL locking coverage for Phase B empty-pallet cleanup."""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.automatic_empty_pallet_return_service import AutomaticEmptyPalletReturnService
from fms_server.empty_pallet_return_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    EmptyPalletReturnDuplicateError,
    EmptyPalletReturnService,
)
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.operator_empty_pallet_return_service import OperatorEmptyPalletReturnService
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    JobStatus,
    MaterialDeliveryStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.execution_attempt_service import ExecutionAttemptService


pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    if not test_url or make_url(test_url).database != EXPECTED_DATABASE:
        pytest.fail("Empty-pallet PostgreSQL tests require guarded smart_factory_benchmark.")
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


def _service(session: Session) -> EmptyPalletReturnService:
    coordinator = ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(session),
    )
    return EmptyPalletReturnService(session, forklift_execution_coordinator=coordinator)


def _coordinator(session: Session) -> ForkliftExecutionCoordinator:
    return ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(session),
    )


def _auto_service(session: Session) -> AutomaticEmptyPalletReturnService:
    return AutomaticEmptyPalletReturnService(
        session,
        forklift_execution_coordinator_factory=_coordinator,
        runtime_enabled=True,
    )


def test_two_postgres_sessions_claim_only_one_empty_return(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"RETURN_PG_{suffix}"
    job_code = f"RETURN_PG_JOB_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Empty return PostgreSQL fixture")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=job_code, product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code=f"{job_code}-DEL-1",
            display_name="Completed transported delivery",
            status=MaterialDeliveryStatus.COMPLETED,
            supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="OUTER_WALLS",
            supply_destination_code="DROP",
        )
        session.add(delivery)
        session.flush()
        session.add(
            ExecutionAttempt(
                req_id=f"RETURN_PG_MATERIAL_{suffix}",
                executor_type=ExecutorType.FORKLIFT,
                command_type="EXECUTE_TRANSPORT",
                job_id=job.job_id,
                job_delivery_id=delivery.job_delivery_id,
                attempt_no=1,
                status=ExecutionAttemptStatus.SUCCEEDED,
                request_payload_json=json.dumps({
                    "job_id": job.job_id,
                    "delivery_id": delivery.job_delivery_id,
                    "pickup_code": "RACK1",
                    "dropoff_code": "DROP",
                }),
            )
        )
        session.commit()
        delivery_id = delivery.job_delivery_id
        job_id = job.job_id

    barrier = threading.Barrier(2)

    def claim_once() -> str:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                _service(session).claim_empty_pallet_return(job_delivery_id=delivery_id)
                return "CLAIMED"
            except EmptyPalletReturnDuplicateError:
                return "DUPLICATE"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: claim_once(), range(2)))
        assert sorted(outcomes) == ["CLAIMED", "DUPLICATE"]

        with session_factory() as session:
            attempts = list(session.scalars(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.job_delivery_id == delivery_id,
                    ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
                )
            ))
            assert len(attempts) == 1
            assert attempts[0].req_id
            assert attempts[0].status.value == "CREATED"
            assert session.get(JobMaterialDelivery, delivery_id).status is MaterialDeliveryStatus.COMPLETED
    finally:
        with session_factory() as session:
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == delivery_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
            session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()


def test_auto_and_operator_return_race_create_one_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"RETURN_RACE_{suffix}"
    part_code = f"RETURN_RACE_PART_{suffix}"
    job_code = f"RETURN_RACE_JOB_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="Auto/manual return race")
        part = Part(
            part_code=part_code, part_name="Auto/manual return race part",
            category=PartCategory.STRUCTURE, vision_class=f"return_race_{suffix}", unit="EA",
        )
        session.add_all((product, part))
        session.flush()
        job = ProductionJob(job_code=job_code, product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        step = JobStep(
            job_id=job.job_id, step_order=1, operation_code="INSTALL_TEST",
            display_name="Consumed transported part", status=StepStatus.COMPLETED,
        )
        session.add(step)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id, batch_order=1,
            delivery_code=f"{job_code}-DEL-1", display_name="Completed transported delivery",
            status=MaterialDeliveryStatus.COMPLETED, supply_mode=SupplyMode.TRANSPORTED,
            supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
        )
        session.add(delivery)
        session.flush()
        session.add(JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id,
            part_code=part_code, quantity=1,
        ))
        session.add(ExecutionAttempt(
            req_id=f"RETURN_RACE_MATERIAL_{suffix}", executor_type=ExecutorType.FORKLIFT,
            command_type="EXECUTE_TRANSPORT", job_id=job.job_id,
            job_delivery_id=delivery.job_delivery_id, attempt_no=1,
            status=ExecutionAttemptStatus.SUCCEEDED,
            request_payload_json=json.dumps({
                "job_id": job.job_id, "delivery_id": delivery.job_delivery_id,
                "pickup_code": "RACK1", "dropoff_code": "DROP",
            }),
        ))
        session.commit()
        job_id = job.job_id
        delivery_id = delivery.job_delivery_id

    barrier = threading.Barrier(2)

    def auto_once() -> str:
        with session_factory() as session:
            barrier.wait(timeout=10)
            return "AUTO" if _auto_service(session).dispatch_one_eligible_return() else "AUTO_BLOCKED"

    def operator_once() -> str:
        with session_factory() as session:
            barrier.wait(timeout=10)
            try:
                result = OperatorEmptyPalletReturnService(
                    session, forklift_execution_coordinator=_coordinator(session)
                ).execute_confirmed_empty_return(
                    production_job_id=job_id, job_delivery_id=delivery_id
                )
                return result.status.value
            except EmptyPalletReturnDuplicateError:
                return "OPERATOR_BLOCKED"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda fn: fn(), (auto_once, operator_once)))
        assert len([outcome for outcome in outcomes if outcome in {"AUTO", "SUCCEEDED"}]) == 1
        with session_factory() as session:
            attempts = list(session.scalars(select(ExecutionAttempt).where(
                ExecutionAttempt.job_delivery_id == delivery_id,
                ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
            )))
            assert len(attempts) == 1
            assert attempts[0].status is ExecutionAttemptStatus.SUCCEEDED
            assert session.get(JobMaterialDelivery, delivery_id).status is MaterialDeliveryStatus.COMPLETED
    finally:
        with session_factory() as session:
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery_id))
            session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id == delivery_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == delivery_id))
            session.execute(delete(JobStep).where(JobStep.job_id == job_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
            session.execute(delete(Part).where(Part.part_code == part_code))
            session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()
