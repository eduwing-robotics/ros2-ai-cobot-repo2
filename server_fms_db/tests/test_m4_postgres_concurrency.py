"""PostgreSQL-only M4 allocation concurrency coverage.

These checks deliberately do not use SQLite: the production allocator relies
on PostgreSQL partial unique indexes to detect and retry attempt-number races.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    StepStatus,
)
from shared.services.execution_attempt_service import (
    ExecutionAttemptConflictError,
    ExecutionAttemptService,
)


pytestmark = pytest.mark.postgres_integration

EXPECTED_TEST_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"
WORKER_COUNT = 5


def _test_database_url() -> str:
    """Return only the explicitly configured, guarded PostgreSQL test target."""
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")

    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    production_url = settings.database_url.strip()
    if not test_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured.")

    parsed_test_url = make_url(test_url)
    if parsed_test_url.get_backend_name() != "postgresql":
        pytest.fail("POSTGRES_TEST_DATABASE_URL must use PostgreSQL.")
    if parsed_test_url.database != EXPECTED_TEST_DATABASE:
        pytest.fail(
            "Refusing M4 PostgreSQL concurrency tests: target must be "
            f"{EXPECTED_TEST_DATABASE!r}."
        )
    if production_url and test_url == production_url:
        pytest.fail("Refusing M4 PostgreSQL concurrency tests: test URL equals DATABASE_URL.")
    return test_url


@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[Engine]:
    engine = create_engine(_test_database_url(), pool_size=WORKER_COUNT, max_overflow=WORKER_COUNT, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert connection.dialect.name == "postgresql"
            assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_TEST_DATABASE
            assert connection.execute(text("select version_num from alembic_version")).scalar_one() == EXPECTED_REVISION
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=postgres_engine, autoflush=False, expire_on_commit=False)


@dataclass(frozen=True)
class AttemptTarget:
    product_code: str
    job_id: int
    job_step_id: int
    job_delivery_id: int


@pytest.fixture
def attempt_target(session_factory: sessionmaker[Session]) -> Iterator[AttemptTarget]:
    suffix = uuid.uuid4().hex[:16].upper()
    product_code = f"M4PG_{suffix}"
    job_code = f"M4PG_JOB_{suffix}"

    with session_factory() as session:
        product = Product(product_code=product_code, product_name="M4 PostgreSQL concurrency fixture")
        job = ProductionJob(job_code=job_code, product_code=product_code, status=JobStatus.REQUESTED)
        session.add_all((product, job))
        session.flush()
        step = JobStep(
            job_id=job.job_id,
            step_order=1,
            operation_code="M4_CONCURRENT_STEP",
            display_name="M4 concurrent step",
            status=StepStatus.PENDING,
        )
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code=f"M4PG_DELIVERY_{suffix}",
            display_name="M4 concurrent delivery",
            status=MaterialDeliveryStatus.PENDING,
        )
        session.add_all((step, delivery))
        session.flush()
        target = AttemptTarget(
            product_code=product_code,
            job_id=job.job_id,
            job_step_id=step.job_step_id,
            job_delivery_id=delivery.job_delivery_id,
        )
        session.commit()

    try:
        yield target
    finally:
        # Delete only rows created by this fixture, in FK-safe order.
        with session_factory() as session:
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id == target.job_id))
            session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == target.job_delivery_id))
            session.execute(delete(JobStep).where(JobStep.job_step_id == target.job_step_id))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id == target.job_id))
            session.execute(delete(Product).where(Product.product_code == target.product_code))
            session.commit()


def _concurrent_attempt_numbers(
    *,
    session_factory: sessionmaker[Session],
    target: AttemptTarget,
    executor_type: ExecutorType,
    command_type: str,
    use_delivery: bool,
) -> list[tuple[int, str, int]]:
    """Issue five real service calls from separate sessions/connections."""
    barrier = threading.Barrier(WORKER_COUNT)

    def create_attempt(worker_index: int) -> tuple[int, str, int]:
        with session_factory() as session:
            barrier.wait(timeout=10)
            kwargs: dict[str, int] = (
                {"job_delivery_id": target.job_delivery_id}
                if use_delivery
                else {"job_step_id": target.job_step_id}
            )
            attempt = ExecutionAttemptService(session).create_attempt(
                executor_type=executor_type,
                command_type=command_type,
                request_payload={"fixture": "m4-postgres", "worker": worker_index},
                job_id=target.job_id,
                req_id=f"m4-pg-{command_type.lower()}-{uuid.uuid4()}",
                **kwargs,
            )
            return attempt.attempt_id, attempt.req_id, attempt.attempt_no

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
        futures = [executor.submit(create_attempt, index) for index in range(WORKER_COUNT)]
        return [future.result(timeout=20) for future in futures]


def _assert_distinct_attempts(results: list[tuple[int, str, int]]) -> None:
    assert len(results) == WORKER_COUNT
    assert len({attempt_id for attempt_id, _, _ in results}) == WORKER_COUNT
    assert len({req_id for _, req_id, _ in results}) == WORKER_COUNT
    assert sorted(attempt_no for _, _, attempt_no in results) == [1, 2, 3, 4, 5]


def test_execution_attempt_indexes_are_real_postgresql_partial_guards(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        definitions = dict(
            connection.execute(
                text(
                    "select indexname, indexdef from pg_indexes "
                    "where schemaname = 'public' and tablename = 'execution_attempts'"
                )
            ).all()
        )

    assert "(job_step_id, command_type, attempt_no)" in definitions[
        "uq_execution_attempts_job_step_command_attempt"
    ]
    assert "WHERE (job_step_id IS NOT NULL)" in definitions[
        "uq_execution_attempts_job_step_command_attempt"
    ]
    assert "(job_delivery_id, command_type, attempt_no)" in definitions[
        "uq_execution_attempts_job_delivery_command_attempt"
    ]
    assert "WHERE (job_delivery_id IS NOT NULL)" in definitions[
        "uq_execution_attempts_job_delivery_command_attempt"
    ]
    assert "execution_attempts_req_id_key" in definitions


def test_concurrent_job_step_attempt_allocation_in_postgresql(
    session_factory: sessionmaker[Session], attempt_target: AttemptTarget
) -> None:
    results = _concurrent_attempt_numbers(
        session_factory=session_factory,
        target=attempt_target,
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="M4_CONCURRENT_STEP",
        use_delivery=False,
    )
    _assert_distinct_attempts(results)


def test_concurrent_delivery_attempt_allocation_in_postgresql(
    session_factory: sessionmaker[Session], attempt_target: AttemptTarget
) -> None:
    results = _concurrent_attempt_numbers(
        session_factory=session_factory,
        target=attempt_target,
        executor_type=ExecutorType.FORKLIFT,
        command_type="M4_CONCURRENT_DELIVERY",
        use_delivery=True,
    )
    _assert_distinct_attempts(results)


def test_req_id_idempotency_and_payload_conflict_in_postgresql(
    session_factory: sessionmaker[Session], attempt_target: AttemptTarget
) -> None:
    req_id = f"m4-pg-idempotency-{uuid.uuid4()}"
    payload = {"fixture": "m4-postgres", "value": 1}
    with session_factory() as session:
        service = ExecutionAttemptService(session)
        first = service.create_attempt(
            executor_type=ExecutorType.ROBOT_CELL,
            command_type="M4_REQ_ID",
            request_payload=payload,
            job_id=attempt_target.job_id,
            job_step_id=attempt_target.job_step_id,
            req_id=req_id,
        )
        same = service.create_attempt(
            executor_type=ExecutorType.ROBOT_CELL,
            command_type="M4_REQ_ID",
            request_payload=payload,
            job_id=attempt_target.job_id,
            job_step_id=attempt_target.job_step_id,
            req_id=req_id,
        )
        assert same.attempt_id == first.attempt_id
        with pytest.raises(ExecutionAttemptConflictError):
            service.create_attempt(
                executor_type=ExecutorType.ROBOT_CELL,
                command_type="M4_REQ_ID",
                request_payload={"fixture": "m4-postgres", "value": 2},
                job_id=attempt_target.job_id,
                job_step_id=attempt_target.job_step_id,
                req_id=req_id,
            )
