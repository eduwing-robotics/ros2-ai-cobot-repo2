"""Guarded smart_factory_benchmark validation for the HOUSE_B MVP master seed."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from fms_server.transport_location_resolver import resolve_policy_transport_locations
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE, seed_house_b_mvp_master
from shared.config import get_settings
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    ExecutionAttempt,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStep,
    MaterialInspection,
    Part,
    ProductionEvent,
    ProductionInspection,
    ProductionJob,
    RoofOptionCode,
)
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.production_cell_payload_builder import ProductionCellPayloadBuilder
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService

pytestmark = pytest.mark.postgres_integration
EXPECTED_DATABASE = "smart_factory_benchmark"


def _database_url() -> str:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")
    settings = get_settings()
    benchmark = settings.postgres_test_database_url.strip()
    production = settings.database_url.strip()
    if not benchmark or make_url(benchmark).database != EXPECTED_DATABASE:
        pytest.fail("HOUSE_B PostgreSQL seed tests require smart_factory_benchmark.")
    if not production or benchmark == production:
        pytest.fail("POSTGRES_TEST_DATABASE_URL must be non-empty and distinct from DATABASE_URL.")
    return benchmark


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(_database_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_database()")) == EXPECTED_DATABASE
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()


def _cleanup_job(session: Session, job_id: int) -> None:
    delivery_ids = list(session.scalars(select(JobMaterialDelivery.job_delivery_id).where(
        JobMaterialDelivery.production_job_id == job_id
    )))
    step_ids = list(session.scalars(select(JobStep.job_step_id).where(JobStep.job_id == job_id)))
    if delivery_ids:
        session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(select(
            JobMaterialDeliveryItem.delivery_item_id
        ).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)))))
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id.in_(delivery_ids)))
        session.execute(delete(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids)))
        session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)))
        session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(delivery_ids)))
    session.execute(delete(ProductionInspection).where(ProductionInspection.production_job_id == job_id))
    session.execute(delete(ProductionEvent).where(ProductionEvent.job_id == job_id))
    if step_ids:
        session.execute(delete(JobStep).where(JobStep.job_step_id.in_(step_ids)))
    session.execute(delete(ProductionJob).where(ProductionJob.job_id == job_id))
    session.commit()


def test_house_b_seed_is_idempotent_and_builds_job_delivery_payload_and_qa_snapshots(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        # The production-safe seed function owns no default DATABASE_URL path.
        first = seed_house_b_mvp_master(session)
        session.commit()
        second = seed_house_b_mvp_master(session)
        session.commit()
        assert first.recipe_id == second.recipe_id
        recipe = session.scalar(select(AssemblyRecipe).where(
            AssemblyRecipe.product_code == HOUSE_B_PRODUCT_CODE,
            AssemblyRecipe.is_active.is_(True),
        ))
        assert recipe is not None and recipe.recipe_id == first.recipe_id
        stages = list(session.scalars(select(AssemblyRecipeStage).where(
            AssemblyRecipeStage.recipe_id == recipe.recipe_id
        ).order_by(AssemblyRecipeStage.stage_order)))
        assert len(stages) == 7
        assert [stage.operation_code for stage in stages] == [
            "INSTALL_BASE",
            "INSTALL_REAR_OUTER_WALL",
            "INSTALL_DOOR_OUTER_WALL",
            "INSTALL_LEFT_OUTER_WALL",
            "INSTALL_RIGHT_OUTER_WALL",
            "INSTALL_INNER_WALL",
            "INSTALL_ROOF",
        ]
        assert [stage.supply_group_code for stage in stages[1:5]] == ["OUTER_WALLS"] * 4
        assert stages[5].supply_group_code == "INNER_WALL"

        job = ProductionOrchestrationService(session).create_job(
            product_code=HOUSE_B_PRODUCT_CODE,
            job_code=f"HOUSE-B-BENCH-{uuid.uuid4().hex[:16]}",
            roof_option_code=RoofOptionCode.ROOF_02,
        )
        try:
            steps = list(session.scalars(select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)))
            assert len(steps) == 6
            deliveries = list(session.scalars(select(JobMaterialDelivery).where(
                JobMaterialDelivery.production_job_id == job.job_id
            )))
            by_group = {delivery.supply_group_code: delivery for delivery in deliveries}
            assert set(by_group) == {"BASE", "INNER_WALL", "OUTER_WALLS", "ROOF"}
            roof_item = session.scalar(select(JobMaterialDeliveryItem).where(
                JobMaterialDeliveryItem.job_delivery_id == by_group["ROOF"].job_delivery_id
            ))
            assert roof_item is not None and roof_item.part_code == "ROOF-ZIP-01" and roof_item.job_step_id is None
            inner = resolve_policy_transport_locations(by_group["INNER_WALL"])
            outer = resolve_policy_transport_locations(by_group["OUTER_WALLS"])
            assert (inner.pickup_code, inner.dropoff_code) == ("RACK2", "DROP")
            assert (outer.pickup_code, outer.dropoff_code) == ("RACK1", "DROP")
            outer_items = list(session.scalars(select(JobMaterialDeliveryItem).where(
                JobMaterialDeliveryItem.job_delivery_id == by_group["OUTER_WALLS"].job_delivery_id
            )))
            assert len(outer_items) == 4
            builder = ProductionCellPayloadBuilder(session)
            inner_step = next(step for step in steps if step.operation_code == "INSTALL_INNER_WALL")
            outer_step = next(step for step in steps if step.operation_code == "INSTALL_REAR_OUTER_WALL")
            assert builder.build_for_job_step(job_id=job.job_id, job_step_id=inner_step.job_step_id).parts[0].class_name == "wall_int"
            assert builder.build_for_job_step(job_id=job.job_id, job_step_id=outer_step.job_step_id).parts[0].class_name == "wall_ext"
            assert session.scalar(select(func.count()).select_from(MaterialInspection).where(
                MaterialInspection.delivery_item_id.in_(select(JobMaterialDeliveryItem.delivery_item_id).where(
                    JobMaterialDeliveryItem.job_delivery_id.in_([delivery.job_delivery_id for delivery in deliveries])
                ))
            )) == 0
            qa_item = outer_items[0]
            qa = MaterialInspectionService().request_inspection(session, qa_item.delivery_item_id)
            part = session.get(Part, qa_item.part_code)
            assert part is not None
            assert (qa.expected_part_code, qa.expected_class_name) == (part.part_code, part.vision_class)
            roof_qa = MaterialInspectionService().request_inspection(session, roof_item.delivery_item_id)
            assert (roof_qa.expected_part_code, roof_qa.expected_class_name, roof_qa.expected_quantity) == (
                "ROOF-ZIP-01", "roof_zip", 1
            )
            session.commit()
            roof_item_id = roof_item.delivery_item_id
            roof_qa_id = session.scalar(select(MaterialInspection.inspection_id).where(
                MaterialInspection.delivery_item_id == roof_item_id
            ))

            orchestration = ProductionOrchestrationService(session)
            orchestration.start_job(job.job_id)
            for _ in range(6):
                step = orchestration.get_next_step(job.job_id)
                assert step is not None
                orchestration.start_step(step.job_step_id)
                orchestration.complete_step(step.job_step_id)
            lifecycle = ProductionCompletionService(session)
            lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
            roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
            session.refresh(roof_item)
            assert roof_item.job_step_id == roof_step.job_step_id
            assert session.scalar(select(func.count()).select_from(JobMaterialDelivery).where(
                JobMaterialDelivery.production_job_id == job.job_id,
                JobMaterialDelivery.supply_group_code == "ROOF",
            )) == 1
            assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem).where(
                JobMaterialDeliveryItem.job_delivery_id == by_group["ROOF"].job_delivery_id
            )) == 1
            assert session.scalar(select(MaterialInspection.inspection_id).where(
                MaterialInspection.inspection_id == roof_qa_id,
                MaterialInspection.delivery_item_id == roof_item_id,
            )) == roof_qa_id
        finally:
            _cleanup_job(session, job.job_id)
