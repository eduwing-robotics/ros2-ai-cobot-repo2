"""PostgreSQL-only exactly-once coverage for durable automatic ReturnHome."""

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

from fms_server.automatic_return_home_service import (
    AutomaticReturnHomeService,
    RETURN_HOME_COMMAND_TYPE,
)
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
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
        pytest.fail("ReturnHome PostgreSQL tests require guarded smart_factory_benchmark.")
    if test_url == settings.database_url.strip():
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


def _factory(transport: FakeForkliftActionTransport):
    def coordinator(session: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(
            session,
            adapter=ForkliftActionAdapter(transport),
            execution_attempt_service=ExecutionAttemptService(session),
        )
    return coordinator


def test_two_postgres_workers_dispatch_exactly_one_return_home(
    session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = f"HOME_PG_{suffix}"
    job_code = f"HOME_PG_JOB_{suffix}"
    with session_factory() as session:
        product = Product(product_code=product_code, product_name="ReturnHome PostgreSQL fixture")
        session.add(product)
        session.flush()
        job = ProductionJob(job_code=job_code, product_code=product.product_code, status=JobStatus.RUNNING)
        session.add(job)
        session.flush()
        for batch_order, group, rack in ((1, "INNER_WALL", "RACK2"), (2, "OUTER_WALLS", "RACK1")):
            delivery = JobMaterialDelivery(
                production_job_id=job.job_id,
                batch_order=batch_order,
                delivery_code=f"{job_code}-{group}",
                display_name=group,
                status=MaterialDeliveryStatus.COMPLETED,
                supply_mode=SupplyMode.TRANSPORTED,
                supply_group_code=group,
                supply_destination_code="DROP",
            )
            session.add(delivery)
            session.flush()
            session.add_all((
                ExecutionAttempt(
                    req_id=f"HOME_PG_MATERIAL_{group}_{suffix}", executor_type=ExecutorType.FORKLIFT,
                    command_type="EXECUTE_TRANSPORT", job_id=job.job_id,
                    job_delivery_id=delivery.job_delivery_id, attempt_no=1,
                    status=ExecutionAttemptStatus.SUCCEEDED,
                    request_payload_json=json.dumps({"pickup_code": rack, "dropoff_code": "DROP"}),
                ),
                ExecutionAttempt(
                    req_id=f"HOME_PG_RETURN_{group}_{suffix}", executor_type=ExecutorType.FORKLIFT,
                    command_type="EXECUTE_TRANSPORT_EMPTY_RETURN", job_id=job.job_id,
                    job_delivery_id=delivery.job_delivery_id, attempt_no=1,
                    status=ExecutionAttemptStatus.SUCCEEDED,
                    request_payload_json=json.dumps({"pickup_code": "DROP", "dropoff_code": rack}),
                ),
            ))
        session.commit()
        job_id = job.job_id

    barrier = threading.Barrier(2)

    def dispatch_once() -> bool:
        with session_factory() as session:
            transport = FakeForkliftActionTransport()
            service = AutomaticReturnHomeService(
                session,
                forklift_execution_coordinator_factory=_factory(transport),
                runtime_enabled=True,
            )
            barrier.wait(timeout=10)
            return service.dispatch_one_eligible_return_home()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: dispatch_once(), range(2)))
        assert sorted(outcomes) == [False, True]
        with session_factory() as session:
            attempts = list(session.scalars(select(ExecutionAttempt).where(
                ExecutionAttempt.job_id == job_id,
                ExecutionAttempt.command_type == RETURN_HOME_COMMAND_TYPE,
            )))
            assert len(attempts) == 1
            assert attempts[0].status is ExecutionAttemptStatus.SUCCEEDED
    finally:
        with session_factory() as session:
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id == job_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
            session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()
