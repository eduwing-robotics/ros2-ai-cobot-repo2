"""Opt-in PostgreSQL integration coverage for ProductionOrchestrationService.
These tests never use DATABASE_URL. They require both RUN_POSTGRES_INTEGRATION=1
and POSTGRES_TEST_DATABASE_URL pointing exactly to smart_factory_benchmark.
"""
from __future__ import annotations
import asyncio
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload, sessionmaker
from api_server.services.production_conversation_service import ProductionConversationService
from shared.services.production_cell_payload_builder import ProductionCellPayloadBuilder
from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import CoordinatorOutcome, FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.material_feed_execution_coordinator import (
    MaterialFeedCoordinatorOutcome,
    MaterialFeedExecutionCoordinator,
)
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, RobotCellTaskTypeMapper
from scripts.run_fake_production_demo import run_fake_production_demo
from shared.config import get_settings
from shared.enums.ai import Intent
from tests.recipe_test_support import add_active_recipe, add_gated_roof_stages, seed_complete_incoming_qa
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    MaterialInspection,
    MaterialDeliveryStatus,
    Part,
    PartCategory,
    PendingProductionRequest,
    PendingProductionState,
    InspectionStatus,
    Inventory,
    EventType,
    ExecutionAttempt,
    JobStatus,
    JobStep,
    Product, InstallationSlot,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionResult,
    ProductionInspectionStatus,
    ProductionJob,
    RoofOptionCode,
    StepStatus,
)
from shared.schemas.ai import StructuredCommand
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService
from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotService
from shared.services.pending_production_request_service import (
    ActivePendingProductionRequestExistsError,
    PendingProductionRequestService,
)
from shared.services.production_request_materialization_service import ProductionRequestMaterializationService
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightResult
from shared.services.production_configuration_validator import ProductionConfigurationInvalidError
from shared.services.production_completion_service import (
    InvalidProductionCompletionTransitionError,
    ProductionCompletionService,
)
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionOrchestrationService,
)
pytestmark = pytest.mark.postgres_integration
EXPECTED_TEST_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "20260904_02"
PROCESS_STEP_SEED_CODES = [
    "MATERIAL_DELIVERY", "INSTALL_FLOOR", "INSTALL_WALL", "INSTALL_ROOF",
    "INSTALL_WINDOW", "ELECTRICAL_WIRING", "PLUMBING", "INTERIOR_FINISH", "QUALITY_INSPECTION",
]
EXPECTED_STEP_CODES = [f"TEST_OPERATION_{index:02d}" for index in range(1, 17)]
def _test_database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    test_url = settings.postgres_test_database_url.strip()
    development_url = settings.database_url.strip()
    if not test_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured.")
    parsed_test_url = make_url(test_url)
    if parsed_test_url.get_backend_name() != "postgresql":
        pytest.fail("POSTGRES_TEST_DATABASE_URL must use PostgreSQL.")
    if parsed_test_url.database != EXPECTED_TEST_DATABASE:
        pytest.fail(
            "Refusing PostgreSQL integration tests: the test URL must target "
            f"{EXPECTED_TEST_DATABASE!r}."
        )
    if development_url and test_url == development_url:
        pytest.fail("Refusing PostgreSQL integration tests: test URL equals DATABASE_URL.")
    return test_url
@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[Engine]:
    engine = create_engine(_test_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert connection.dialect.name == "postgresql"
            assert connection.execute(text("select current_database()")).scalar_one() == EXPECTED_TEST_DATABASE
            revision = connection.execute(text("select version_num from alembic_version")).scalar_one_or_none()
            assert revision == EXPECTED_REVISION, (
                "The dedicated test database is not migrated to the expected revision. "
                "Run: .venv/bin/python scripts/setup_postgres_integration_db.py"
            )
            seeded_step_count = connection.execute(text("select count(*) from assembly_recipe_stages")).scalar_one()
            assert seeded_step_count > 0
        yield engine
    finally:
        engine.dispose()
@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=postgres_engine, autoflush=False, expire_on_commit=False)
@pytest.fixture
def test_product_code(session_factory: sessionmaker[Session]) -> Iterator[int]:
    suffix = uuid.uuid4().hex[:12].upper()
    with session_factory() as session:
        product = Product(
            product_code=f"PGIT_HOUSE_{suffix}",
            product_name="PostgreSQL integration test house",
        )
        session.add(product)
        session.flush()
        add_gated_roof_stages(session, add_active_recipe(session, product.product_code))
        session.commit()
        product_code = product.product_code
    try:
        yield product_code
    finally:
        # Delete only rows created for this test product; never truncate or touch
        # reference seed data or other test data.
        with session_factory() as session:
            product_code = session.scalar(
                select(Product.product_code).where(Product.product_code == product_code)
            )
            job_ids = list(
                session.scalars(
                    select(ProductionJob.job_id).where(ProductionJob.product_code == product_code)
                )
            )
            if job_ids:
                job_step_ids = list(session.scalars(select(JobStep.job_step_id).where(JobStep.job_id.in_(job_ids))))
                job_delivery_ids = list(session.scalars(select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id.in_(job_ids))))
                session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id.in_(job_ids)))
                if job_step_ids:
                    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_step_id.in_(job_step_ids)))
                if job_delivery_ids:
                    delivery_item_ids = list(session.scalars(select(JobMaterialDeliveryItem.delivery_item_id).where(JobMaterialDeliveryItem.job_delivery_id.in_(job_delivery_ids))))
                    if delivery_item_ids:
                        session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(delivery_item_ids)))
                    session.execute(delete(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id.in_(job_delivery_ids)))
                    session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id.in_(job_delivery_ids)))
                    session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(job_delivery_ids)))
                inspection_ids = list(session.scalars(select(ProductionInspection.inspection_id).where(ProductionInspection.production_job_id.in_(job_ids))))
                if inspection_ids:
                    session.execute(delete(ProductionInspectionResult).where(ProductionInspectionResult.inspection_id.in_(inspection_ids)))
                session.execute(delete(ProductionInspection).where(ProductionInspection.production_job_id.in_(job_ids)))
                session.execute(delete(ProductionEvent).where(ProductionEvent.job_id.in_(job_ids)))
                session.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids)))
                session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
            if product_code is not None:
                session.execute(
                    delete(PendingProductionRequest).where(
                        PendingProductionRequest.product_code == product_code
                    )
                )
                recipe_ids = list(session.scalars(select(AssemblyRecipe.recipe_id).where(AssemblyRecipe.product_code == product_code)))
                if recipe_ids:
                    recipe_part_codes = list(
                        session.scalars(
                            select(AssemblyRecipeStage.part_code).where(
                                AssemblyRecipeStage.recipe_id.in_(recipe_ids),
                                AssemblyRecipeStage.part_code.is_not(None),
                            )
                        )
                    )
                    session.execute(delete(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id.in_(recipe_ids)))
                    session.execute(delete(AssemblyRecipe).where(AssemblyRecipe.recipe_id.in_(recipe_ids)))
                    if recipe_part_codes:
                        session.execute(delete(Inventory).where(Inventory.part_code.in_(recipe_part_codes)))
                        session.execute(delete(Part).where(Part.part_code.in_(recipe_part_codes)))

                # Cleanup InstallationSlot added by tests
                session.execute(delete(InstallationSlot).where(InstallationSlot.product_code == product_code))

                session.execute(delete(Product).where(Product.product_code == product_code))
            session.commit()
def _create_job(
    session_factory: sessionmaker[Session], *, product_code: int, suffix: str | None = None, roof_option_code: RoofOptionCode | None = RoofOptionCode.ROOF_01
) -> int:
    job_suffix = suffix or uuid.uuid4().hex[:12].upper()
    with session_factory() as session:
        job = ProductionOrchestrationService(session).create_job(
            product_code=product_code,
            job_code=f"{product_code}-{job_suffix}",
            roof_option_code=roof_option_code,
        )
        return job.job_id
def _ordered_steps(session_factory: sessionmaker[Session], job_id: int) -> list[JobStep]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(JobStep)
                .where(JobStep.job_id == job_id)
                .order_by(JobStep.step_order)
            )
        )
def _event_types(session_factory: sessionmaker[Session], job_id: int) -> list[EventType]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(ProductionEvent.event_type)
                .where(ProductionEvent.job_id == job_id)
                .order_by(ProductionEvent.event_id)
            )
        )
def _run_service_call(
    session_factory: sessionmaker[Session], operation: Callable[[ProductionOrchestrationService], object]
) -> object:
    with session_factory() as session:
        return operation(ProductionOrchestrationService(session))
def test_migration_seeded_house_recipes_and_active_unique_constraint_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    with session_factory() as session:
        for product_code, expected_version, expected_stage_count in (
            ("HOUSE_A", 1, 16),
            # HOUSE_B MVP is an immutable v2 recipe: six initial stages plus
            # one PRE_ROOF_PASS-gated roof stage.
            ("HOUSE_B", 2, 7),
        ):
            recipes = list(
                session.scalars(
                    select(AssemblyRecipe).where(
                        AssemblyRecipe.product_code == product_code,
                        AssemblyRecipe.is_active.is_(True),
                    )
                )
            )
            assert len(recipes) == 1
            assert recipes[0].version == expected_version
            assert session.scalar(
                select(func.count())
                .select_from(AssemblyRecipeStage)
                .where(AssemblyRecipeStage.recipe_id == recipes[0].recipe_id)
            ) == expected_stage_count
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        session.add(
            AssemblyRecipe(
                product_code=product_code,
                version=2,
                is_active=True,
                description="conflicting PostgreSQL test recipe",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
def test_create_job_initializes_seeded_steps_and_created_event(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.REQUESTED
    steps = _ordered_steps(session_factory, job_id)
    assert [step.operation_code for step in steps] == EXPECTED_STEP_CODES
    assert [step.status for step in steps] == [StepStatus.PENDING] * 16
    assert _event_types(session_factory, job_id) == [EventType.JOB_CREATED]
def test_create_job_persists_nullable_roof_option_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code, suffix="ROOF-01")
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        assert job is not None
        assert job.roof_option_code is RoofOptionCode.ROOF_01
    with session_factory() as session:
        job = ProductionOrchestrationService(session).create_job(
            product_code=test_product_code,
            job_code=f"PGIT-JOB-ROOF-{uuid.uuid4().hex[:12].upper()}",
            roof_option_code=RoofOptionCode.ROOF_02,
        )
        job_id = job.job_id
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        assert job is not None
        assert job.roof_option_code is RoofOptionCode.ROOF_02
def test_start_and_complete_step_commit_real_postgresql_transaction(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    first_step_id = _ordered_steps(session_factory, job_id)[0].job_step_id
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    _run_service_call(session_factory, lambda service: service.start_step(first_step_id))
    _run_service_call(session_factory, lambda service: service.complete_step(first_step_id))
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        step = session.get(JobStep, first_step_id)
        assert job is not None and job.status is JobStatus.RUNNING
        assert step is not None and step.status is StepStatus.COMPLETED
        assert step.started_at is not None and step.completed_at is not None
    assert _event_types(session_factory, job_id) == [
        EventType.JOB_CREATED,
        EventType.JOB_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
    ]
def test_fail_step_updates_step_job_and_events_in_one_postgresql_transaction(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    first_step_id = _ordered_steps(session_factory, job_id)[0].job_step_id
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    _run_service_call(session_factory, lambda service: service.start_step(first_step_id))
    _run_service_call(
        session_factory,
        lambda service: service.fail_step(first_step_id, reason="PostgreSQL integration failure", error_code="E503"),
    )
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        step = session.get(JobStep, first_step_id)
        assert job is not None and job.status is JobStatus.FAILED
        assert step is not None and step.status is StepStatus.FAILED
        assert step.failure_reason == "PostgreSQL integration failure"
    assert _event_types(session_factory, job_id)[-2:] == [
        EventType.STEP_FAILED,
        EventType.JOB_FAILED,
    ]
    with session_factory() as session:
        step_failed = session.scalar(
            select(ProductionEvent).where(
                ProductionEvent.job_id == job_id,
                ProductionEvent.event_type == EventType.STEP_FAILED,
            )
        )
        job_failed = session.scalar(
            select(ProductionEvent).where(
                ProductionEvent.job_id == job_id,
                ProductionEvent.event_type == EventType.JOB_FAILED,
            )
        )
        assert step_failed is not None and step_failed.error_code == "E503"
        assert job_failed is not None and job_failed.error_code is None
def test_event_write_failure_rolls_back_job_state_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    session = session_factory()
    service = ProductionOrchestrationService(session)
    def fail_event_write(**kwargs: object) -> ProductionEvent:
        raise RuntimeError("simulated PostgreSQL event insert failure")
    monkeypatch.setattr(service, "_record_event", fail_event_write)
    try:
        with pytest.raises(RuntimeError, match="event insert failure"):
            service.start_job(job_id)
    finally:
        session.close()
    with session_factory() as verify_session:
        job = verify_session.get(ProductionJob, job_id)
        assert job is not None and job.status is JobStatus.REQUESTED
    assert _event_types(session_factory, job_id) == [EventType.JOB_CREATED]
def test_last_completed_step_marks_job_completed_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    for step in _ordered_steps(session_factory, job_id):
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.start_step(step_id))
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.complete_step(step_id))
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        assert job is not None and job.status is JobStatus.PRE_ROOF_READY
        assert job.completed_at is None
    assert _event_types(session_factory, job_id)[-2:] == [
        EventType.STEP_COMPLETED,
        EventType.PRE_ROOF_READY,
    ]
def test_pre_roof_inspection_and_roof_completion_lifecycle_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(
        session_factory,
        product_code=test_product_code,
        suffix="ROOF-LIFECYCLE",
        roof_option_code=RoofOptionCode.ROOF_02,
    )
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    for step in _ordered_steps(session_factory, job_id):
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.start_step(step_id))
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.complete_step(step_id))
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        inspection = session.scalar(
            select(ProductionInspection).where(ProductionInspection.production_job_id == job_id)
        )
        assert job is not None and job.status is JobStatus.PRE_ROOF_READY
        assert job.completed_at is None
        assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    with session_factory() as session:
        ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job_id)
    with session_factory() as session:
        roof_step = ProductionCompletionService(session).pass_pre_roof_inspection(production_job_id=job_id)
        assert session.get(ProductionJob, job_id).roof_option_code is RoofOptionCode.ROOF_02
        assert roof_step.step_order == 18
        assert roof_step.status is StepStatus.PENDING
        roof_step_id = roof_step.job_step_id
    with session_factory() as session:
        session.add(
            JobStep(
                job_id=job_id,
                source_recipe_stage_id=roof_step.source_recipe_stage_id,
                step_order=19,
                operation_code="INSTALL_ROOF",
                display_name="duplicate roof step",
                status=StepStatus.PENDING,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    with session_factory() as session:
        ProductionOrchestrationService(session).start_step(roof_step_id)
    with session_factory() as session:
        ProductionOrchestrationService(session).complete_step(roof_step_id)
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        roof_step = session.get(JobStep, roof_step_id)
        assert job is not None and job.status is JobStatus.COMPLETED and job.completed_at is not None
        assert roof_step is not None and roof_step.status is StepStatus.COMPLETED
    assert _event_types(session_factory, job_id).count(EventType.JOB_COMPLETED) == 1
@pytest.mark.parametrize("product_code", ["HOUSE_A", "HOUSE_B"])
def test_fake_demo_runner_executes_seeded_house_recipe_until_current_readiness_gate_in_postgresql(
    session_factory: sessionmaker[Session], product_code: str,
) -> None:
    job_id: int | None = None
    try:
        with session_factory() as session:
            recipe = session.scalar(
                select(AssemblyRecipe).where(
                    AssemblyRecipe.product_code == product_code,
                    AssemblyRecipe.is_active.is_(True),
                )
            )
            assert recipe is not None
            expected_stages = sorted(recipe.stages, key=lambda stage: stage.stage_order)
            if not any(stage.part_code is not None and stage.quantity is not None and stage.quantity > 0 for stage in expected_stages):
                with pytest.raises(ProductionConfigurationInvalidError):
                    run_fake_production_demo(session, product_code=product_code, delay_seconds=0)
                return
            if product_code == "HOUSE_B":
                with pytest.raises(ProductionConfigurationInvalidError):
                    run_fake_production_demo(session, product_code=product_code, delay_seconds=0)
                return
            result = run_fake_production_demo(session, product_code=product_code, delay_seconds=0)
            job_id = result.job_id
            job = session.get(ProductionJob, job_id)
            inspection = session.scalar(
                select(ProductionInspection).where(ProductionInspection.production_job_id == job_id)
            )
            # The full master includes a PRE_ROOF_PASS-gated Roof stage; it is
            # intentionally absent from the initial JobStep snapshot.
            initial_stages = [stage for stage in expected_stages if stage.execution_gate is None]
            assert result.recipe_stage_count == len(initial_stages)
            first_material_index = next((index for index, stage in enumerate(initial_stages) if stage.part_code is not None and stage.quantity is not None and stage.quantity > 0), None)
            if first_material_index is None:
                pytest.fail("A production recipe with no material requirements must be rejected before demo dispatch.")
            else:
                blocked_stage = initial_stages[first_material_index]
                assert result.action_commands == ()
                assert result.stopped_outcome is CoordinatorOutcome.NOT_DISPATCHED_NOT_READY
                assert job is not None and job.status is JobStatus.RUNNING
                blocked_step = session.scalar(select(JobStep).where(JobStep.job_id == job_id, JobStep.step_order == blocked_stage.stage_order))
                assert blocked_step is not None and blocked_step.status is StepStatus.PENDING
                readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(job_id=job_id, job_step_id=blocked_step.job_step_id)
                expected_reason = StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
                assert readiness.reason is expected_reason
                assert inspection is None
    finally:
        if job_id is not None:
            with session_factory() as session:
                step_ids = list(
                    session.scalars(select(JobStep.job_step_id).where(JobStep.job_id == job_id))
                )
                delivery_ids = list(
                    session.scalars(
                        select(JobMaterialDelivery.job_delivery_id).where(
                            JobMaterialDelivery.production_job_id == job_id
                        )
                    )
                )
                session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id == job_id))
                if step_ids:
                    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_step_id.in_(step_ids)))
                if delivery_ids:
                    delivery_item_ids = list(session.scalars(select(JobMaterialDeliveryItem.delivery_item_id).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids))))
                    if delivery_item_ids:
                        session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(delivery_item_ids)))
                    session.execute(
                        delete(JobMaterialFeedExecution).where(
                            JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids)
                        )
                    )
                    session.execute(
                        delete(JobMaterialDeliveryItem).where(
                            JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
                        )
                    )
                    session.execute(
                        delete(JobMaterialDelivery).where(
                            JobMaterialDelivery.job_delivery_id.in_(delivery_ids)
                        )
                    )
                inspection_ids = list(session.scalars(select(ProductionInspection.inspection_id).where(ProductionInspection.production_job_id == job_id)))
                if inspection_ids:
                    session.execute(delete(ProductionInspectionResult).where(ProductionInspectionResult.inspection_id.in_(inspection_ids)))
                session.execute(
                    delete(ProductionInspection).where(
                        ProductionInspection.production_job_id == job_id
                    )
                )
                session.execute(delete(ProductionEvent).where(ProductionEvent.job_id == job_id))
                session.execute(delete(JobStep).where(JobStep.job_id == job_id))
                session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
                session.commit()
def test_execution_snapshot_reads_postgresql_state_without_mutation(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code, suffix="SNAPSHOT", roof_option_code=RoofOptionCode.ROOF_02)
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    with session_factory() as session:
        before_events = session.scalar(
            select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id == job_id)
        )
        snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=job_id)
        after_events = session.scalar(
            select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id == job_id)
        )
        assert snapshot.job_status is JobStatus.RUNNING
        assert snapshot.current_step is None
        assert snapshot.next_step is not None
        assert snapshot.next_step.ready is False
        assert snapshot.next_step.readiness_reason == "PRE_PRODUCTION_QA_INCOMPLETE"
        assert snapshot.next_step.dispatchable is False
        assert snapshot.next_step.dispatch_block_reason == "NOT_READY"
        assert before_events == after_events
    with session_factory() as session:
        service = ProductionOrchestrationService(session)
        step = service.get_next_step(job_id)
        assert step is not None
        service.start_step(step.job_step_id)
        service.fail_step(step.job_step_id, reason="snapshot failure", error_code="E503")
    with session_factory() as session:
        snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=job_id)
        assert snapshot.job_status is JobStatus.FAILED
        assert snapshot.current_step is None and snapshot.next_step is None
        assert snapshot.last_event is not None and snapshot.last_event.event_type is EventType.JOB_FAILED
        assert snapshot.last_step_execution_event is not None
        assert snapshot.last_step_execution_event.event_type is EventType.STEP_FAILED
        assert snapshot.last_step_execution_event.error_code == "E503"
def test_for_update_row_lock_blocks_second_transition(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    first_step_id = _ordered_steps(session_factory, job_id)[0].job_step_id
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    lock_holder = session_factory()
    contender = session_factory()
    try:
        locked_step = lock_holder.scalar(
            select(JobStep).where(JobStep.job_step_id == first_step_id).with_for_update()
        )
        assert locked_step is not None
        contender.execute(text("set local lock_timeout = '250ms'"))
        with pytest.raises(OperationalError) as error:
            ProductionOrchestrationService(contender).start_step(first_step_id)
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
    finally:
        lock_holder.rollback()
        contender.close()
        lock_holder.close()
    with session_factory() as session:
        step = session.get(JobStep, first_step_id)
        assert step is not None and step.status is StepStatus.PENDING
    assert EventType.STEP_STARTED not in _event_types(session_factory, job_id)
def test_concurrent_step_transition_allows_exactly_one_success(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    job_id = _create_job(session_factory, product_code=test_product_code)
    first_step_id = _ordered_steps(session_factory, job_id)[0].job_step_id
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()
    def attempt_transition() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            ProductionOrchestrationService(session).start_step(first_step_id)
            outcome = "started"
        except InvalidProductionStateTransitionError:
            outcome = "rejected"
        finally:
            session.close()
        with outcomes_lock:
            outcomes.append(outcome)
    threads = [threading.Thread(target=attempt_transition) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["rejected", "started"]
    with session_factory() as session:
        step = session.get(JobStep, first_step_id)
        assert step is not None and step.status is StepStatus.RUNNING
    assert _event_types(session_factory, job_id).count(EventType.STEP_STARTED) == 1
def test_pending_request_persists_enum_and_nullable_roof_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    with session_factory() as session:
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        pending = PendingProductionRequestService(session, ttl_seconds=300).create_pending(
            session_id=f"PGIT-PENDING-{uuid.uuid4().hex[:12]}",
            product_code=product_code,
            quantity=2,
        )
        request_id = pending.request_id
    with session_factory() as session:
        stored = session.get(PendingProductionRequest, request_id)
        assert stored is not None
        assert stored.state is PendingProductionState.WAITING_ROOF_OPTION
        assert stored.roof_option_code is None
        assert stored.quantity == 2
        changed = PendingProductionRequestService(session).set_roof_option(
            request_id=request_id,
            roof_option_code=RoofOptionCode.ROOF_02,
        )
        assert changed.state is PendingProductionState.AWAITING_CONFIRMATION
        assert changed.roof_option_code is RoofOptionCode.ROOF_02
def test_pending_active_session_constraint_and_service_rollback_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    with session_factory() as session:
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        session_id = f"PGIT-ACTIVE-{uuid.uuid4().hex[:12]}"
        service = PendingProductionRequestService(session)
        first = service.create_pending(
            session_id=session_id, product_code=product_code, quantity=1
        )
        with pytest.raises(ActivePendingProductionRequestExistsError):
            service.create_pending(session_id=session_id, product_code=product_code, quantity=1)
        assert session.get(PendingProductionRequest, first.request_id) is not None
        assert session.scalar(
            select(PendingProductionRequest.request_id).where(
                PendingProductionRequest.session_id == session_id
            )
        ) == first.request_id
    # This bypasses service validation to prove that PostgreSQL's partial unique
    # index remains the final race-condition defense.
    with session_factory() as session:
        session_id = f"PGIT-INDEX-{uuid.uuid4().hex[:12]}"
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        session.add(
            PendingProductionRequest(
                session_id=session_id,
                state=PendingProductionState.WAITING_ROOF_OPTION,
                product_code=product_code,
                quantity=1,
                expires_at=expiry,
            )
        )
        session.commit()
        session.add(
            PendingProductionRequest(
                session_id=session_id,
                state=PendingProductionState.AWAITING_CONFIRMATION,
                product_code=product_code,
                quantity=1,
                roof_option_code=RoofOptionCode.ROOF_01,
                expires_at=expiry,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
def test_pending_expiry_is_persisted_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    current = {"value": datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)}
    with session_factory() as session:
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        service = PendingProductionRequestService(
            session, ttl_seconds=1, clock=lambda: current["value"]
        )
        pending = service.create_pending(
            session_id=f"PGIT-EXPIRY-{uuid.uuid4().hex[:12]}",
            product_code=product_code,
            quantity=1,
        )
        request_id = pending.request_id
        current["value"] = current["value"] + timedelta(seconds=2)
        assert service.get_active_by_session(pending.session_id) is None
    with session_factory() as session:
        stored = session.get(PendingProductionRequest, request_id)
        assert stored is not None
        assert stored.state is PendingProductionState.EXPIRED
def test_pending_conversation_materializes_current_recipe_and_rejects_before_job_creation_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    class FakeInterpreter:
        def __init__(self, command: StructuredCommand) -> None:
            self.command = command
            self.calls = 0
        async def interpret(self, text: str):
            self.calls += 1
            return text, self.command, "{}"
    with session_factory() as session:
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        interpreter = FakeInterpreter(
            StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_name="PostgreSQL integration test house",
                product_code=product_code,
                quantity=1,
                requires_confirmation=True,
            )
        )
        conversation = ProductionConversationService(
            pending_service=PendingProductionRequestService(session),
            interpreter=interpreter,
            materialization_service=ProductionRequestMaterializationService(session),
        )
        jobs_before = session.scalar(select(func.count()).select_from(ProductionJob))
        waiting = asyncio.run(
            conversation.handle_text(session_id=f"PGIT-CONFIRM-{uuid.uuid4().hex[:12]}", text="생산 요청")
        )
        assert waiting.pending is not None
        assert waiting.pending.state is PendingProductionState.WAITING_ROOF_OPTION
        assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before
        confirmation = asyncio.run(
            conversation.handle_text(session_id=waiting.session_id, text="2번")
        )
        assert confirmation.pending is not None
        assert confirmation.pending.state is PendingProductionState.AWAITING_CONFIRMATION
        assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before
        confirmed = asyncio.run(conversation.handle_text(session_id=waiting.session_id, text="네"))
        assert confirmed.pending is not None
        assert confirmed.pending.state is PendingProductionState.CONFIRMED
        assert confirmed.pending.confirmed_at is not None
        assert len(confirmed.production_jobs) == 1
        job = confirmed.production_jobs[0]
        assert job.assembly_recipe_id is not None
        assert job.roof_option_code is RoofOptionCode.ROOF_02
        assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before + 1
        recipe = session.get(AssemblyRecipe, job.assembly_recipe_id)
        assert recipe is not None
        assert recipe.product_code == product_code
        recipe_stages = list(
            session.scalars(
                select(AssemblyRecipeStage)
                .where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)
                .order_by(AssemblyRecipeStage.stage_order)
            )
        )
        job_steps = list(
            session.scalars(
                select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)
            )
        )
        assert [
            (step.step_order, step.operation_code, step.display_name, step.source_recipe_stage_id)
            for step in job_steps
        ] == [
            (stage.stage_order, stage.operation_code, stage.display_name, stage.recipe_stage_id)
            for stage in recipe_stages if stage.execution_gate is None
        ]
        assert all(step.source_recipe_stage_id is not None for step in job_steps)
        assert not any(step.operation_code == "INSTALL_ROOF" for step in job_steps)
        deliveries = list(session.scalars(
            select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id)
        ))
        assert len(deliveries) == 2
        deferred_roof_delivery = next(
            delivery for delivery in deliveries if delivery.supply_group_code == "TEST_ROOF_GROUP_ROOF_02"
        )
        deferred_roof_item = session.scalar(select(JobMaterialDeliveryItem).where(
            JobMaterialDeliveryItem.job_delivery_id == deferred_roof_delivery.job_delivery_id
        ))
        assert deferred_roof_item is not None and deferred_roof_item.job_step_id is None
        replay = ProductionRequestMaterializationService(session).confirm_and_create_jobs(
            pending_request_id=confirmed.pending.request_id
        )
        assert replay.created is False
        assert [existing.job_id for existing in replay.jobs] == [job.job_id]
        rejection = asyncio.run(
            conversation.handle_text(session_id=f"PGIT-REJECT-{uuid.uuid4().hex[:12]}", text="새 생산 요청")
        )
        assert rejection.pending is not None
        asyncio.run(conversation.handle_text(session_id=rejection.session_id, text="1번"))
        rejected = asyncio.run(conversation.handle_text(session_id=rejection.session_id, text="아니"))
        assert rejected.pending is not None
        assert rejected.pending.state is PendingProductionState.REJECTED
        assert rejected.pending.rejected_at is not None
        assert rejected.pending.confirmed_at is None
        assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before + 1
def test_material_delivery_snapshot_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as session:
        product = session.get(Product, test_product_code)
        assert product is not None
        recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == product.product_code))
        assert recipe is not None
        part = Part(
        vision_class="wall_ext_back",
            part_code=f"PGIT-PART-{uuid.uuid4().hex[:10].upper()}",
            part_name="PostgreSQL delivery part",
            category=PartCategory.STRUCTURE,
            unit="EA",
        )
        session.add(part)
        session.flush()
        for stage in recipe.stages:
            if stage.stage_order == 1:
                stage.part_code = part.part_code
                stage.quantity = 1
            else:
                stage.part_code = None
                stage.quantity = None
        session.flush()
        job = ProductionOrchestrationService(session).create_job(
            product_code=test_product_code, job_code=f"PGIT-DELIVERY-{uuid.uuid4().hex[:12]}", roof_option_code=RoofOptionCode.ROOF_01
        )
        try:
            deliveries = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)
            assert len(job.steps) == 16 and len(deliveries) == 1
            first_step = sorted(job.steps, key=lambda step: step.step_order)[0]
            readiness = StepReadinessService(MaterialDeliveryService(session))
            assert readiness.evaluate(job_id=job.job_id, job_step_id=first_step.job_step_id).ready is False
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise e
        assert readiness.evaluate(job_id=job.job_id, job_step_id=first_step.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
        first_delivery = deliveries[0]
        delivery_service = MaterialDeliveryService(session)
        delivery_service.start_delivery(first_delivery.job_delivery_id)
        delivery_service.complete_delivery(first_delivery.job_delivery_id)
        assert readiness.evaluate(job_id=job.job_id, job_step_id=first_step.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
        inspection_service = MaterialInspectionService()
        for item in first_delivery.items:
            request = inspection_service.request_inspection(session, item.delivery_item_id)
            inspection_service.mark_running(session, request.inspection_request_id)
            inspection_service.apply_inspection_result(
                session,
                IncomingMaterialQAResult(
                    inspection_request_id=request.inspection_request_id,
                    delivery_item_id=request.delivery_item_id,
                    inspection_cycle=request.inspection_cycle,
                    status="COMPLETED",
                    result="PASS",
                    expected_part_code=request.expected_part_code,
                    expected_class_name=request.expected_class_name,
                    expected_quantity=request.expected_quantity,
                    detected_quantity=request.expected_quantity,
                    detections=[],
                    frame_width=640,
                    frame_height=480,
                    camera_source="GLOBAL_CAMERA",
                    frame_seq=1,
                    timestamp=datetime.now(timezone.utc),
                    model_scope="postgres-test",
                    model_version="1",
                    production_valid=True,
                ),
            )
        assert readiness.evaluate(job_id=job.job_id, job_step_id=first_step.job_step_id).reason is StepReadinessReason.MATERIAL_FEED_NOT_READY
        feed_service = MaterialFeedExecutionService(session)
        feed = feed_service.get_for_delivery(first_delivery.job_delivery_id)
        assert feed is not None
        feed_transport = FakeCellActionTransport([
            FakeCellActionExchange(CellTaskExecutionResult.success(completed_slots=("SYNTHETIC_FEED_SLOT",)))
        ])
        feed_result = MaterialFeedExecutionCoordinator(
            session, robot_cell_adapter=RobotCellActionAdapter(feed_transport)
        ).execute_feed(
            feed_execution_id=feed.feed_execution_id,
            req_id="pgit-feed-1",
            parts_json='[{"slot":"SYNTHETIC_FEED_SLOT","class":"furniture_bath"}]',
        )
        assert feed_result.outcome is MaterialFeedCoordinatorOutcome.COMPLETED
        assert feed_transport.commands[0].task_type == "MATERIAL_FEED"
        stored_feed = session.get(JobMaterialFeedExecution, feed.feed_execution_id)
        assert stored_feed is not None and stored_feed.completed_json == '["SYNTHETIC_FEED_SLOT"]'
        assert readiness.evaluate(job_id=job.job_id, job_step_id=first_step.job_step_id).ready is True
        # Obsolete plan logic removed
        pending = PendingProductionRequestService(session).create_pending(
            session_id=f"PGIT-DELIVERY-ROLLBACK-{uuid.uuid4().hex[:12]}",
            product_code=product.product_code,
            quantity=2,
            roof_option_code=RoofOptionCode.ROOF_01,
        )
        request_id = pending.request_id
        original_snapshot = MaterialDeliveryService.instantiate_for_job
        calls = {"count": 0}
        monkeypatch.setattr(
            "shared.services.production_inventory_preflight_service.ProductionInventoryPreflightService.validate",
            lambda *args, **kwargs: ProductionInventoryPreflightResult(
                can_produce=True,
                product_code=product.product_code,
                requested_quantity=2,
                shortages=[],
            ),
        )
        def fail_second_snapshot(self, **kwargs):
            calls["count"] += 1
            result = original_snapshot(self, **kwargs)
            if calls["count"] == 2:
                raise RuntimeError("forced PostgreSQL delivery snapshot failure")
            return result
        monkeypatch.setattr(MaterialDeliveryService, "instantiate_for_job", fail_second_snapshot)
        with pytest.raises(RuntimeError, match="forced PostgreSQL delivery snapshot failure"):
            ProductionRequestMaterializationService(session).confirm_and_create_jobs(
                pending_request_id=request_id
            )
        session.expire_all()
        rolled_back_pending = session.get(PendingProductionRequest, request_id)
        assert rolled_back_pending is not None
        assert rolled_back_pending.state is PendingProductionState.AWAITING_CONFIRMATION
        assert rolled_back_pending.confirmed_at is None
        assert session.scalar(
            select(func.count()).select_from(ProductionJob).where(
                ProductionJob.source_pending_request_id == request_id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(JobStep).join(ProductionJob).where(
                ProductionJob.source_pending_request_id == request_id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(JobMaterialDelivery).join(ProductionJob).where(
                ProductionJob.source_pending_request_id == request_id
            )
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .join(ProductionJob)
            .where(ProductionJob.source_pending_request_id == request_id)
        ) == 0
def test_recipe_materialization_second_job_failure_rolls_back_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory() as session:
        product_code = session.scalar(
            select(Product.product_code).where(Product.product_code == test_product_code)
        )
        assert product_code is not None
        pending = PendingProductionRequestService(session).create_pending(
            session_id=f"PGIT-ROLLBACK-{uuid.uuid4().hex[:12]}",
            product_code=product_code,
            quantity=2,
            roof_option_code=RoofOptionCode.ROOF_02,
        )
        request_id = pending.request_id
    session = session_factory()
    service = ProductionRequestMaterializationService(session)
    original_create_job = service._create_job
    calls = {"count": 0}
    def fail_second_job(**kwargs: object) -> ProductionJob:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("forced PostgreSQL second-job failure")
        return original_create_job(**kwargs)
    monkeypatch.setattr(service, "_create_job", fail_second_job)
    try:
        with pytest.raises(RuntimeError, match="forced PostgreSQL second-job failure"):
            service.confirm_and_create_jobs(pending_request_id=request_id)
    finally:
        session.close()
    with session_factory() as verify_session:
        pending = verify_session.get(PendingProductionRequest, request_id)
        assert pending is not None
        assert pending.state is PendingProductionState.AWAITING_CONFIRMATION
        assert pending.confirmed_at is None
        assert verify_session.scalar(
            select(func.count())
            .select_from(ProductionJob)
            .where(ProductionJob.source_pending_request_id == request_id)
        ) == 0
        assert verify_session.scalar(
            select(func.count())
            .select_from(JobStep)
            .join(ProductionJob)
            .where(ProductionJob.source_pending_request_id == request_id)
        ) == 0
def test_pending_materialization_trace_idempotency_unique_constraint_and_concurrency_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    with session_factory() as session:
        product_code = session.scalar(select(Product.product_code).where(Product.product_code == test_product_code))
        assert product_code is not None
        pending = PendingProductionRequestService(session).create_pending(
            session_id=f"PGIT-MATERIAL-{uuid.uuid4().hex[:12]}",
            product_code=product_code,
            quantity=2,
            roof_option_code=RoofOptionCode.ROOF_01,
        )
        request_id = pending.request_id
    barrier = threading.Barrier(3)
    outcomes: list[int] = []
    errors: list[Exception] = []
    outcomes_lock = threading.Lock()
    def materialize() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            result = ProductionRequestMaterializationService(session).confirm_and_create_jobs(
                pending_request_id=request_id
            )
            with outcomes_lock:
                outcomes.append(len(result.jobs))
        except Exception as error:  # pragma: no cover - assertion below reports it
            with outcomes_lock:
                errors.append(error)
        finally:
            session.close()
    threads = [threading.Thread(target=materialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert outcomes == [2, 2]
    with session_factory() as session:
        jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.source_pending_request_id == request_id).order_by(ProductionJob.source_item_index)))
        assert [job.source_item_index for job in jobs] == [1, 2]
        assert all(job.roof_option_code is RoofOptionCode.ROOF_01 for job in jobs)
        assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id.in_([job.job_id for job in jobs]))) == 32
        assert session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id.in_([job.job_id for job in jobs]), ProductionEvent.event_type == EventType.JOB_CREATED)) == 2
        duplicate = ProductionJob(
            job_code=f"PGIT-DUP-{uuid.uuid4().hex[:12]}",
            product_code=test_product_code,
            roof_option_code=RoofOptionCode.ROOF_01,
            source_pending_request_id=request_id,
            source_item_index=1,
            status=JobStatus.REQUESTED,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
def test_fms_execution_coordinator_blocks_unapproved_recipe_operation_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str
) -> None:
    """Generic test recipes intentionally remain outside the 7 approved Cell mappings."""
    with session_factory() as session:
        product = session.get(Product, test_product_code)
        assert product is not None
        orchestration = ProductionOrchestrationService(session)
        job = orchestration.create_job(
            product_code=product.product_code,
            job_code=f"PGIT-COORD-{uuid.uuid4().hex[:12]}",
            roof_option_code=RoofOptionCode.ROOF_01,
        )
        orchestration.start_job(job.job_id)
        seed_complete_incoming_qa(session, job_id=job.job_id)
        step = orchestration.get_next_step(job.job_id)
        assert step is not None and step.operation_code not in RobotCellTaskTypeMapper._OPERATION_TO_TASK_TYPE
        transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success())])
        from shared.services.execution_attempt_service import ExecutionAttemptService
        coordinator = FmsExecutionCoordinator(
            session,
            orchestration_service=orchestration,
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=RobotCellActionAdapter(transport),
            execution_attempt_service=ExecutionAttemptService(session),
        )
        outcome = coordinator.execute_step(
            job_id=job.job_id,
            job_step_id=step.job_step_id,
            req_id=f"pgit-job-{job.job_id}-step-{step.job_step_id}",
            parts_json='[{"slot":"PGIT_SYNTHETIC","class":"base"}]',
        )
        assert outcome.outcome is CoordinatorOutcome.CONTRACT_BLOCKED
        assert session.get(JobStep, step.job_step_id).status is StepStatus.PENDING
        assert session.get(ProductionJob, job.job_id).status is JobStatus.RUNNING
        assert transport.commands == []
def _cleanup_snapshot_owned_graph(
    session_factory: sessionmaker[Session], *, product_code: str, part_code: str, slot_code: str
) -> None:
    """Delete only one SNAP test graph, in FK-dependent order."""
    with session_factory() as session:
        job_ids = list(session.scalars(
            select(ProductionJob.job_id).where(ProductionJob.product_code == product_code)
        ))
        if job_ids:
            step_ids = list(session.scalars(
                select(JobStep.job_step_id).where(JobStep.job_id.in_(job_ids))
            ))
            delivery_ids = list(session.scalars(
                select(JobMaterialDelivery.job_delivery_id).where(
                    JobMaterialDelivery.production_job_id.in_(job_ids)
                )
            ))
            session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_id.in_(job_ids)))
            if step_ids:
                session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_step_id.in_(step_ids)))
            if delivery_ids:
                item_ids = list(session.scalars(
                    select(JobMaterialDeliveryItem.delivery_item_id).where(
                        JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
                    )
                ))
                if item_ids:
                    session.execute(delete(MaterialInspection).where(
                        MaterialInspection.delivery_item_id.in_(item_ids)
                    ))
                session.execute(delete(JobMaterialFeedExecution).where(
                    JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids)
                ))
                session.execute(delete(JobMaterialDeliveryItem).where(
                    JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
                ))
                session.execute(delete(JobMaterialDelivery).where(
                    JobMaterialDelivery.job_delivery_id.in_(delivery_ids)
                ))
            inspection_ids = list(session.scalars(
                select(ProductionInspection.inspection_id).where(
                    ProductionInspection.production_job_id.in_(job_ids)
                )
            ))
            if inspection_ids:
                session.execute(delete(ProductionInspectionResult).where(
                    ProductionInspectionResult.inspection_id.in_(inspection_ids)
                ))
            session.execute(delete(ProductionInspection).where(
                ProductionInspection.production_job_id.in_(job_ids)
            ))
            session.execute(delete(ProductionEvent).where(ProductionEvent.job_id.in_(job_ids)))
            session.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids)))
            session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))

        recipe_ids = list(session.scalars(
            select(AssemblyRecipe.recipe_id).where(AssemblyRecipe.product_code == product_code)
        ))
        if recipe_ids:
            session.execute(delete(AssemblyRecipeStage).where(
                AssemblyRecipeStage.recipe_id.in_(recipe_ids)
            ))
            session.execute(delete(AssemblyRecipe).where(AssemblyRecipe.recipe_id.in_(recipe_ids)))
        session.execute(delete(InstallationSlot).where(InstallationSlot.slot_code == slot_code))
        session.execute(delete(Part).where(Part.part_code == part_code))
        session.execute(delete(Product).where(Product.product_code == product_code))
        session.commit()


def _create_snapshot_job(session: Session, *, suffix: str):
    """Create one complete SNAP graph and commit before any test assertion."""
    product_code = f"SNAP-PRODUCT-{suffix}"
    part_code = f"SNAP-PART-{suffix}"
    slot_code = f"SNAP-SLOT-{suffix}"
    product = Product(product_code=product_code, product_name="Snap")
    part = Part(
        vision_class="wall_ext_back", part_code=part_code, part_name="Part",
        category="STRUCTURE", unit="EA",
    )
    session.add_all([product, part])
    session.flush()
    slot = InstallationSlot(product_code=product_code, slot_code=slot_code, display_name="Test")
    session.add(slot)
    session.flush()
    recipe = AssemblyRecipe(product_code=product_code, is_active=True, version=1)
    session.add(recipe)
    session.flush()
    stage = AssemblyRecipeStage(
        recipe_id=recipe.recipe_id, stage_order=1, operation_code="INSTALL_DOOR",
        display_name="Test Op", part_code=part_code, quantity=1,
        slot_code=slot_code, pick_zone="ZONE_1", is_terminal=True,
    )
    session.add(stage)
    session.commit()
    job = ProductionOrchestrationService(session).create_job(
        product_code=product_code, job_code=f"SNAP-JOB-{suffix}"
    )
    return job, stage, (product_code, part_code, slot_code)


def _assert_snapshot_owned_graph_absent(
    session_factory: sessionmaker[Session], *, product_code: str, part_code: str, slot_code: str
) -> None:
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Product).where(
            Product.product_code == product_code
        )) == 0
        assert session.scalar(select(func.count()).select_from(Part).where(
            Part.part_code == part_code
        )) == 0
        assert session.scalar(select(func.count()).select_from(InstallationSlot).where(
            InstallationSlot.slot_code == slot_code
        )) == 0
        assert session.scalar(select(func.count()).select_from(ProductionJob).where(
            ProductionJob.product_code == product_code
        )) == 0
        assert session.scalar(select(func.count()).select_from(JobStep).join(ProductionJob).where(
            ProductionJob.product_code == product_code,
            JobStep.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
        )) == 0


def test_job_step_snapshot_stability(session_factory) -> None:
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = part_code = slot_code = ""
    try:
        with session_factory() as session:
            job, stage, (product_code, part_code, slot_code) = _create_snapshot_job(
                session, suffix=suffix
            )
            # Verify durable execution snapshot.
            step = job.steps[0]
            assert step.part_code == part_code
            assert step.slot_code == slot_code
            assert step.pick_zone == "ZONE_1"
            assert step.quantity == 1
            # Changing Recipe master data cannot alter the JobStep snapshot.
            stage.pick_zone = "ZONE_2"
            stage.quantity = 5
            session.commit()
            step_reloaded = session.get(ProductionJob, job.job_id).steps[0]
            assert step_reloaded.pick_zone == "ZONE_1"
            assert step_reloaded.quantity == 1
    finally:
        if product_code:
            _cleanup_snapshot_owned_graph(
                session_factory,
                product_code=product_code,
                part_code=part_code,
                slot_code=slot_code,
            )
    _assert_snapshot_owned_graph_absent(
        session_factory, product_code=product_code, part_code=part_code, slot_code=slot_code
    )


def test_snapshot_graph_cleanup_runs_after_exception(session_factory) -> None:
    """Regression: committed SNAP rows are removed even if a later assertion raises."""
    suffix = uuid.uuid4().hex[:12].upper()
    product_code = part_code = slot_code = ""
    try:
        with session_factory() as session:
            _job, _stage, (product_code, part_code, slot_code) = _create_snapshot_job(
                session, suffix=suffix
            )
            raise RuntimeError("controlled post-commit SNAP assertion failure")
    except RuntimeError as error:
        assert "controlled post-commit" in str(error)
    finally:
        if product_code:
            _cleanup_snapshot_owned_graph(
                session_factory,
                product_code=product_code,
                part_code=part_code,
                slot_code=slot_code,
            )
    _assert_snapshot_owned_graph_absent(
        session_factory, product_code=product_code, part_code=part_code, slot_code=slot_code
    )

def test_pending_production_request_active_session_constraint(session_factory):
    from sqlalchemy.exc import IntegrityError
    from shared.models.factory import PendingProductionRequest
    from datetime import datetime
    import pytest
    with session_factory() as session:
        p1 = PendingProductionRequest(state="AWAITING_CONFIRMATION", session_id="session1", product_code="HOUSE_A", quantity=1, expires_at=datetime.utcnow())
        session.add(p1)
        session.flush()
        p2 = PendingProductionRequest(state="AWAITING_CONFIRMATION", session_id="session1", product_code="HOUSE_B", quantity=1, expires_at=datetime.utcnow())
        session.add(p2)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
@pytest.mark.postgres_integration
def test_job_step_vision_class_snapshot_is_immutable(session_factory: sessionmaker[Session], test_product_code: str) -> None:
    with session_factory() as session:
        # 1. Create Part with vision_class A
        part_code = f"PART-{uuid.uuid4().hex[:6]}"
        part = Part(vision_class="CLASS_A", part_code=part_code, part_name="Test", category="STRUCTURE", unit="EA")
        session.add(part)

        # Add slot and stage
        slot_code = f"SLOT-{uuid.uuid4().hex[:6]}"
        slot = InstallationSlot(product_code=test_product_code, slot_code=slot_code, display_name="Test Slot")
        session.add(slot)

        recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == test_product_code, AssemblyRecipe.is_active.is_(True)))
        assert recipe is not None
        recipe.is_active = False
        session.flush()
        recipe = AssemblyRecipe(product_code=test_product_code, version=recipe.version + 1, is_active=True)
        session.add(recipe)
        session.flush()
        stage = AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=1, operation_code="TEST_OP", display_name="Test", part_code=part_code, quantity=1, slot_code=slot_code, is_terminal=True)
        session.add(stage)
        session.flush()
        stage.is_terminal = True
        session.commit()

        # 2. Materialize JobStep
        service = ProductionOrchestrationService(session)
        job = service.create_job(
            job_code=f"JOB-{uuid.uuid4().hex[:6]}",
            product_code=test_product_code
        )

        # 3. Verify JobStep.vision_class == A
        step = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id, JobStep.operation_code == "TEST_OP"))
        assert step.vision_class == "CLASS_A"

        # 4. Change Part.vision_class to B
        part_to_edit = session.get(Part, part.part_code)
        part_to_edit.vision_class = "CLASS_B"
        session.commit()

        # 5. Verify existing JobStep.vision_class remains A
        session.expire_all()
        step_after = session.get(JobStep, step.job_step_id)
        assert step_after.vision_class == "CLASS_A"

        # 6. Verify newly materialized JobStep uses B
        job2 = service.create_job(
            job_code=f"JOB2-{uuid.uuid4().hex[:6]}",
            product_code=test_product_code
        )
        step2 = session.scalar(select(JobStep).where(JobStep.job_id == job2.job_id, JobStep.operation_code == "TEST_OP"))
        assert step2.vision_class == "CLASS_B"
from scripts.seed_p0_outer_wall_master import seed_p0_outer_walls, PARTS_DATA, SLOTS_DATA

@pytest.mark.postgres_integration
def test_p0_4slot_master_binding_and_payload(session_factory: sessionmaker[Session], test_product_code: str) -> None:
    with session_factory() as session:
        # Create a mock Job #20 equivalent in test DB to verify it remains unchanged
        service = ProductionOrchestrationService(session)
        old_job = service.create_job(
            job_code="JOB-20",
            product_code=test_product_code,
            roof_option_code=RoofOptionCode.ROOF_01
        )
        session.commit()

        old_job_steps = list(session.scalars(select(JobStep).where(JobStep.job_id == old_job.job_id)))
        assert len(old_job_steps) > 0
        old_step_part_codes = {step.job_step_id: step.part_code for step in old_job_steps}
        old_step_slots = {step.job_step_id: step.slot_code for step in old_job_steps}

        # The seed script uses "HOUSE_A". The test DB might only have test_product_code (e.g. PGIT_HOUSE_xxx).
        # We need to adapt the seed script or test environment so the seed script works.
        # Let's seed "HOUSE_A" explicitly if it doesn't exist, since the script relies on it.
        if not session.get(Product, "HOUSE_A"):
            session.add(Product(product_code="HOUSE_A", product_name="HOUSE_A", is_active=True))
            recipe = AssemblyRecipe(product_code="HOUSE_A", version=1, is_active=True, description="test")
            session.add(recipe)
            session.flush()
            # Add the 4 outer wall stages
            stages = [
                (10, "INSTALL_LEFT_OUTER_WALL", "좌측 외벽 설치"),
                (11, "INSTALL_DOOR_OUTER_WALL", "출입문용 외벽 설치"),
                (13, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치"),
                (15, "INSTALL_REAR_OUTER_WALL", "후면 외벽 설치"),
            ]
            for order, op, name in stages:
                session.add(AssemblyRecipeStage(
                    recipe_id=recipe.recipe_id, stage_order=order, operation_code=op, display_name=name, is_terminal=order == max(row[0] for row in stages)
                ))
            session.commit()

        # 1. P0 master seed idempotency
        seed_p0_outer_walls(session)
        seed_p0_outer_walls(session)  # Idempotent call

        # 2. 4 parts exact part_code + vision_class 확인
        for data in PARTS_DATA:
            part = session.get(Part, data["part_code"])
            assert part is not None
            assert part.vision_class == data["vision_class"]

        # 3. 4 slots exact slot_code + HOUSE_A 확인
        for data in SLOTS_DATA:
            slot = session.get(InstallationSlot, data["slot_code"])
            assert slot is not None
            assert slot.product_code == "HOUSE_A"

        # 4. Recipe 4 stage binding 확인
        recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A", AssemblyRecipe.is_active.is_(True)))
        stages = list(session.scalars(select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)))

        bindings = {
            "INSTALL_REAR_OUTER_WALL": ("WALL-EXT-BACK-BLK01", "HOUSE_A_OUTER_WALL_REAR_01"),
            "INSTALL_DOOR_OUTER_WALL": ("WALL-EXT-DOOR-BLK01", "HOUSE_A_OUTER_WALL_DOOR_01"),
            "INSTALL_LEFT_OUTER_WALL": ("WALL-EXT-LEFT-BLK01", "HOUSE_A_OUTER_WALL_LEFT_01"),
            "INSTALL_RIGHT_OUTER_WALL": ("WALL-EXT-RIGHT-BLK01", "HOUSE_A_OUTER_WALL_RIGHT_01"),
        }

        for stage in stages:
            if stage.operation_code in bindings:
                expected_part, expected_slot = bindings[stage.operation_code]
                assert stage.part_code == expected_part
                assert stage.slot_code == expected_slot
                assert stage.pick_zone == "CONVEYOR_PICK"
                assert stage.quantity == 1

        for stage in stages:
            stage.is_terminal = stage.stage_order == max(row.stage_order for row in stages)
        session.flush()

        # 5. 새 Job materialization 시 JobStep에 정확히 snapshot 되는지 확인
        new_job = service.create_job(
            job_code=f"JOB-NEW-{uuid.uuid4().hex[:6]}",
            product_code="HOUSE_A"
        )
        session.commit()

        new_job_steps = list(session.scalars(select(JobStep).where(JobStep.job_id == new_job.job_id)))

        builder = ProductionCellPayloadBuilder(session)

        # 6. payload exact 4 mappings 확인
        for step in new_job_steps:
            if step.operation_code in bindings:
                expected_part, expected_slot = bindings[step.operation_code]
                assert step.part_code == expected_part
                assert step.slot_code == expected_slot
                assert step.pick_zone == "CONVEYOR_PICK"

                # Check Payload matches
                payload = builder.build_for_job_step(job_id=new_job.job_id, job_step_id=step.job_step_id)
                assert len(payload.parts) == 1
                cell_part = payload.parts[0]
                assert cell_part.part_code == expected_part
                assert cell_part.slot == expected_slot
                assert cell_part.zone == "CONVEYOR_PICK"
                # Check vision_class snapshot matches
                part = session.get(Part, expected_part)
                assert step.vision_class == part.vision_class
                # Vision detailed class and Robot Cell manipulation class are
                # different contracts.  Outer-wall installs use coarse wall_ext.
                assert cell_part.class_name == "wall_ext"

        # 7. 기존 Job #20 불변 (status: REQUESTED, mutation: None)
        session.expire_all()
        old_job_after = session.get(ProductionJob, old_job.job_id)
        assert old_job_after.status == JobStatus.REQUESTED
        old_job_steps_after = list(session.scalars(select(JobStep).where(JobStep.job_id == old_job_after.job_id)))
        assert len(old_job_steps_after) == len(old_job_steps)
        for step in old_job_steps_after:
            assert step.part_code == old_step_part_codes[step.job_step_id]
            assert step.slot_code == old_step_slots[step.job_step_id]

        # Clean up
        from shared.models.factory import JobMaterialDelivery, JobMaterialDeliveryItem, JobMaterialFeedExecution
        for j in (new_job, old_job_after):
            if j:
                session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id.in_(
                    select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id == j.job_id)
                )))
                session.execute(delete(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id.in_(
                    select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id == j.job_id)
                )))
                session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == j.job_id))
                session.execute(delete(JobStep).where(JobStep.job_id == j.job_id))
                session.execute(delete(ProductionJob).where(ProductionJob.job_id == j.job_id))
        session.commit()



def test_pre_roof_reinspection_start_and_pass_are_serialized_in_postgresql(
    session_factory: sessionmaker[Session], test_product_code: str,
) -> None:
    """A Job row lock permits one new cycle and one roof materialization."""
    job_id = _create_job(
        session_factory,
        product_code=test_product_code,
        suffix="PRE-ROOF-REINSPECTION-RACE",
        roof_option_code=RoofOptionCode.ROOF_02,
    )
    _run_service_call(session_factory, lambda service: service.start_job(job_id))
    for step in _ordered_steps(session_factory, job_id):
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.start_step(step_id))
        _run_service_call(session_factory, lambda service, step_id=step.job_step_id: service.complete_step(step_id))
    with session_factory() as session:
        lifecycle = ProductionCompletionService(session)
        lifecycle.start_pre_roof_inspection(production_job_id=job_id)
        lifecycle.fail_pre_roof_inspection(production_job_id=job_id, reason="first cycle failed")

    def run_race(operation: str) -> list[str]:
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def invoke() -> None:
            with session_factory() as session:
                barrier.wait(timeout=10)
                try:
                    lifecycle = ProductionCompletionService(session)
                    if operation == "start":
                        lifecycle.start_pre_roof_inspection(production_job_id=job_id)
                    else:
                        lifecycle.pass_pre_roof_inspection(production_job_id=job_id)
                    outcome = "applied"
                except InvalidProductionCompletionTransitionError:
                    outcome = "blocked"
                with outcomes_lock:
                    outcomes.append(outcome)

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        return outcomes

    assert sorted(run_race("start")) == ["applied", "blocked"]
    with session_factory() as session:
        inspections = list(session.scalars(
            select(ProductionInspection)
            .where(ProductionInspection.production_job_id == job_id)
            .order_by(ProductionInspection.inspection_cycle)
        ))
        assert [(row.inspection_cycle, row.status) for row in inspections] == [
            (1, ProductionInspectionStatus.COMPLETED),
            (2, ProductionInspectionStatus.RUNNING),
        ]

    assert sorted(run_race("pass")) == ["applied", "blocked"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobStep).where(
            JobStep.job_id == job_id,
            JobStep.operation_code == "INSTALL_ROOF",
        )) == 1
        latest = session.scalar(
            select(ProductionInspection)
            .where(ProductionInspection.production_job_id == job_id)
            .order_by(ProductionInspection.inspection_cycle.desc())
            .limit(1)
        )
        assert latest is not None and latest.inspection_cycle == 2
        assert latest.status is ProductionInspectionStatus.COMPLETED
        assert latest.result is ProductionInspectionResultCode.PASS
        assert latest.production_valid is True
        assert session.get(ProductionJob, job_id).status is JobStatus.ROOF_READY
