from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    StepStatus,
    SupplyMode,
)
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import seed_inventory_for_recipe


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _recipe(session: Session, *, product_code: str, gated: bool) -> AssemblyRecipe:
    session.add(Product(product_code=product_code, product_name=product_code))
    for index in range(1, 4):
        session.add(Part(
            part_code=f"{product_code}_PART_{index}", part_name=f"Part {index}",
            vision_class=f"class_{index}", category=PartCategory.STRUCTURE, unit="EA",
        ))
    session.flush()
    recipe = AssemblyRecipe(product_code=product_code, version=1, is_active=True)
    session.add(recipe)
    session.flush()
    stages = [
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=1, operation_code="STEP_ALPHA", display_name="Alpha", part_code=f"{product_code}_PART_1", quantity=1),
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=2, operation_code="STEP_BETA", display_name="Beta", part_code=f"{product_code}_PART_2", quantity=1, execution_gate="PRE_ROOF_PASS" if gated else None, supply_mode=SupplyMode.MANUAL if gated else None, supply_group_code="GATED_GROUP" if gated else None, is_terminal=False),
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=3, operation_code="STEP_GAMMA", display_name="Gamma", part_code=f"{product_code}_PART_3", quantity=1, execution_gate="PRE_ROOF_PASS" if gated else None, supply_mode=SupplyMode.MANUAL if gated else None, supply_group_code="GATED_GROUP" if gated else None, is_terminal=True),
    ]
    session.add_all(stages)
    session.flush()
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return recipe


def _pass_item(session: Session, item: JobMaterialDeliveryItem) -> None:
    session.add(MaterialInspection(
        inspection_request_id=f"generic-{item.delivery_item_id}", delivery_item_id=item.delivery_item_id,
        inspection_cycle=1, status=MaterialInspectionStatus.COMPLETED,
        result=MaterialInspectionResult.PASS, expected_part_code=item.part_code,
        expected_class_name=item.part.vision_class, expected_quantity=item.quantity,
        detected_quantity=item.quantity, production_valid=True,
    ))
    session.commit()


def test_non_house_b_expected_items_are_fail_closed_until_every_item_passes() -> None:
    session = _session()
    try:
        _recipe(session, product_code="PRODUCT_X", gated=False)
        job = ProductionOrchestrationService(session).create_job(product_code="PRODUCT_X", job_code="GEN-QA")
        items = list(session.scalars(select(JobMaterialDeliveryItem).join(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id)))
        assert len(items) == 3
        for item in items[:2]:
            _pass_item(session, item)
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id)
        assert readiness.total_items == 3 and readiness.released_items == 2 and readiness.ready is False
        _pass_item(session, items[2])
        assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job.job_id) is True
        orchestration = ProductionOrchestrationService(session)
        orchestration.start_job(job.job_id)
        for expected in ("STEP_ALPHA", "STEP_BETA", "STEP_GAMMA"):
            step = orchestration.get_next_step(job.job_id)
            assert step is not None and step.operation_code == expected
            orchestration.start_step(step.job_step_id)
            orchestration.complete_step(step.job_step_id)
        assert session.get(type(job), job.job_id).status is JobStatus.COMPLETED
    finally:
        session.close()


def test_generic_gated_stages_materialize_and_terminal_metadata_completes_job() -> None:
    session = _session()
    try:
        _recipe(session, product_code="PRODUCT_GATED", gated=True)
        orchestration = ProductionOrchestrationService(session)
        job = orchestration.create_job(product_code="PRODUCT_GATED", job_code="GEN-GATED")
        initial = orchestration.get_next_step(job.job_id)
        assert initial is not None and initial.operation_code == "STEP_ALPHA"
        orchestration.start_job(job.job_id)
        orchestration.start_step(initial.job_step_id)
        orchestration.complete_step(initial.job_step_id)
        assert session.get(type(job), job.job_id).status is JobStatus.PRE_ROOF_READY
        ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
        ProductionCompletionService(session).pass_pre_roof_inspection(production_job_id=job.job_id)
        gated = list(session.scalars(select(type(initial)).where(type(initial).job_id == job.job_id).order_by(type(initial).step_order)))
        assert [step.operation_code for step in gated] == ["STEP_ALPHA", "STEP_BETA", "STEP_GAMMA"]
        assert [step.is_terminal for step in gated] == [False, False, True]
        for expected in ("STEP_BETA", "STEP_GAMMA"):
            step = orchestration.get_next_step(job.job_id)
            assert step is not None and step.operation_code == expected
            orchestration.start_step(step.job_step_id)
            orchestration.complete_step(step.job_step_id)
        assert session.get(type(job), job.job_id).status is JobStatus.COMPLETED
        assert all(step.status is StepStatus.COMPLETED for step in gated)
    finally:
        session.close()
