from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.prepare_operator_gate_rehearsal_job import (
    EXPECTED_REVISION,
    PreparationError,
    _verify_revision,
    prepare_job_to_operator_gate,
)
from scripts.rehearsal_database import RehearsalDatabaseSafetyError, rehearsal_database_url
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE, seed_house_b_mvp_master
from tests.recipe_test_support import seed_inventory_for_recipe
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    ProductionInspection,
    ProductionJob,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    value = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield value
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_job(factory: sessionmaker[Session], code: str) -> ProductionJob:
    with factory() as session:
        recipe = seed_house_b_mvp_master(session)
        seed_inventory_for_recipe(session, recipe)
        session.commit()
        return ProductionOrchestrationService(session).create_job(
            product_code=HOUSE_B_PRODUCT_CODE, job_code=code, roof_option_code=RoofOptionCode.ROOF_02
        )


def test_normal_preparation_stops_at_operator_execution_gate(factory: sessionmaker[Session]) -> None:
    job = _create_job(factory, "OPERATOR-GATE-GOLDEN")
    output: list[str] = []

    result = prepare_job_to_operator_gate(factory, job_id=job.job_id, timeout_seconds=10, emit=output.append)

    with factory() as session:
        stored_job = session.get(ProductionJob, job.job_id)
        target = session.get(JobStep, result.target_step_id)
        assert stored_job is not None and stored_job.status is JobStatus.RUNNING
        assert target is not None
        assert target.status is StepStatus.PENDING
        assert target.supply_mode is SupplyMode.TRANSPORTED
        assert target.operator_execution_ready_at is None
        required = MaterialDeliveryService(session).get_required_deliveries_for_step(target.job_step_id)
        assert required and all(delivery.status.value == "COMPLETED" for delivery in required)
        readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
            job_id=job.job_id, job_step_id=target.job_step_id
        )
        assert readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
        assert session.scalar(
            select(func.count()).select_from(ExecutionAttempt).where(
                ExecutionAttempt.job_step_id == target.job_step_id,
                ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
            )
        ) == 0
        base = session.get(JobStep, result.base_step_id)
        assert base is not None and base.status is StepStatus.COMPLETED
        assert session.scalar(
            select(func.count()).select_from(ProductionInspection).where(
                ProductionInspection.production_job_id == job.job_id
            )
        ) == 0
    assert any("Operator execution release NOT granted" == line for line in output)


def test_gui_condition_is_true_for_prepared_snapshot(factory: sessionmaker[Session]) -> None:
    job = _create_job(factory, "OPERATOR-GATE-GUI")
    result = prepare_job_to_operator_gate(factory, job_id=job.job_id, timeout_seconds=10)
    with factory() as session:
        target = session.get(JobStep, result.target_step_id)
        assert target is not None
        readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
            job_id=job.job_id, job_step_id=target.job_step_id
        )
        assert (
            target.status is StepStatus.PENDING
            and target.supply_mode is SupplyMode.TRANSPORTED
            and target.operator_execution_ready_at is None
            and readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
        )
    page = Path("api_server/static/production_monitor.html").read_text()
    assert "shouldShowOperatorExecutionReady" in page
    assert "조립 시작" in page


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://user:secret@127.0.0.1:5432/smart_factory_db",
    "postgresql+psycopg://user:secret@127.0.0.1:5432/smart_factory_benchmark",
    "postgresql+psycopg://user:secret@127.0.0.1:5432/other_database",
])
def test_non_rehearsal_database_is_rejected_without_connection(url: str) -> None:
    with pytest.raises(RehearsalDatabaseSafetyError):
        rehearsal_database_url(environment={"FACTORY_REHEARSAL_DATABASE_URL": url})


def test_wrong_revision_is_rejected_without_migration() -> None:
    class FakeSession:
        def scalar(self, _query):
            return "old-revision"
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    class FakeFactory:
        def __call__(self): return FakeSession()

    with pytest.raises(PreparationError, match=EXPECTED_REVISION) as raised:
        _verify_revision(FakeFactory())
    assert raised.value.phase == "ALEMBIC_REVISION_MISMATCH"
