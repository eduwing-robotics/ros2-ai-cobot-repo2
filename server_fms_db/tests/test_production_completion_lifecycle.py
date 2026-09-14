from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    EventType,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    Inventory,
    InstallationSlot,
    JobStatus,
    JobStep,

    ProductionInspection,
    ProductionInspectionResult,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    ProductionJob,
    ProductionEvent,
    Part,
    PartCategory,
    Product,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)
from shared.services.production_completion_service import (
    InvalidProductionCompletionTransitionError,
    MissingRoofOptionError,
    ProductionCompletionService,
    RoofStageMaterializationError,
)
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionOrchestrationService,
    UnsupportedExecutionGateError,
)
from tests.recipe_test_support import HOUSE_A_STAGES, HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    db.flush()
    add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES))
    add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_recipe_job(session: Session, *, product_code: str = "HOUSE_A", roof: RoofOptionCode | None = RoofOptionCode.ROOF_01):
    product = session.scalar(select(Product).where(Product.product_code == product_code))
    assert product is not None
    return ProductionOrchestrationService(session).create_job(
        product_code=product.product_code,
        job_code=f"PRE-ROOF-{product_code}-{roof.value if roof else 'NONE'}-{uuid.uuid4().hex[:12]}",
        roof_option_code=roof,
    )


def _complete_assembly(session: Session, job_id: int, *, count: int | None = None) -> None:
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job_id)
    for _ in range(count or 16):
        step = orchestration.get_next_step(job_id)
        assert step is not None
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)


def _job_at_pre_roof(session: Session, *, product_code: str = "HOUSE_A", roof: RoofOptionCode | None = RoofOptionCode.ROOF_01):
    job = _create_recipe_job(session, product_code=product_code, roof=roof)
    _complete_assembly(session, job.job_id)
    session.expire_all()
    return session.get(type(job), job.job_id)


def _roof_step(session: Session, job_id: int) -> JobStep | None:
    return session.scalar(select(JobStep).where(JobStep.job_id == job_id, JobStep.operation_code == "INSTALL_ROOF"))


def _pass_inspection(session: Session, job_id: int) -> JobStep:
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job_id)
    return lifecycle.pass_pre_roof_inspection(production_job_id=job_id)


def test_assembly_completion_creates_one_inspection_but_no_roof_step(session: Session) -> None:
    job = _create_recipe_job(session)
    _complete_assembly(session, job.job_id, count=15)
    assert session.get(type(job), job.job_id).status is JobStatus.RUNNING
    assert _roof_step(session, job.job_id) is None
    assert session.scalar(select(func.count()).select_from(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id)) == 0

    orchestration = ProductionOrchestrationService(session)
    final_step = orchestration.get_next_step(job.job_id)
    assert final_step is not None
    orchestration.start_step(final_step.job_step_id)
    orchestration.complete_step(final_step.job_step_id)

    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert session.get(type(job), job.job_id).status is JobStatus.PRE_ROOF_READY
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    assert _roof_step(session, job.job_id) is None


@pytest.mark.parametrize("roof", [RoofOptionCode.ROOF_01, RoofOptionCode.ROOF_02])
def test_inspection_pass_creates_runtime_roof_step_and_generic_step_lifecycle_completes_job(session: Session, roof: RoofOptionCode) -> None:
    job = _job_at_pre_roof(session, roof=roof)
    assert job is not None
    roof_step = _pass_inspection(session, job.job_id)
    session.expire_all()

    stored = session.get(type(job), job.job_id)
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert stored is not None and stored.status is JobStatus.ROOF_READY and stored.completed_at is None
    assert inspection is not None and inspection.status is ProductionInspectionStatus.COMPLETED
    assert inspection.result is ProductionInspectionResultCode.PASS
    assert inspection.production_valid is True
    assert roof_step.source_recipe_stage_id is not None
    assert roof_step.step_order == (17 if roof is RoofOptionCode.ROOF_01 else 18)
    assert roof_step.operation_code == "INSTALL_ROOF"
    assert roof_step.status is StepStatus.PENDING

    orchestration = ProductionOrchestrationService(session)
    orchestration.start_step(roof_step.job_step_id)
    assert session.get(type(job), job.job_id).status is JobStatus.ROOF_READY
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.RUNNING
    orchestration.complete_step(roof_step.job_step_id)

    # Roof completion now leaves the durable HOUSE_OUTBOUND checkpoint.
    assert session.get(type(job), job.job_id).status is JobStatus.ROOF_READY
    orchestration.complete_job(job.job_id)
    completed = session.get(type(job), job.job_id)
    assert completed is not None and completed.status is JobStatus.COMPLETED and completed.completed_at is not None
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.COMPLETED
    assert session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id == job.job_id, ProductionEvent.event_type == EventType.JOB_COMPLETED)) == 1


def test_runtime_roof_order_uses_max_existing_step_order(session: Session) -> None:
    product = Product(product_code="HOUSE_DYNAMIC", product_name="Dynamic House")
    session.add(product)
    session.flush()
    add_gated_roof_stages(session, add_active_recipe(session, product.product_code, stages=[(1, "INSTALL_BASE", "Base"), (2, "INSTALL_TOILET", "Toilet"), (3, "INSTALL_WINDOW", "Window")]))
    session.commit()
    job = _create_recipe_job(session, product_code="HOUSE_DYNAMIC", roof=RoofOptionCode.ROOF_02)
    _complete_assembly(session, job.job_id, count=3)
    roof_step = _pass_inspection(session, job.job_id)
    assert roof_step.step_order == 5


def test_missing_roof_and_failed_inspection_never_create_or_start_roof_step(session: Session) -> None:
    with pytest.raises(Exception, match="MISSING_TERMINAL_STAGE"):
        _create_recipe_job(session, roof=None)

    lifecycle = ProductionCompletionService(session)
    failed_job = _job_at_pre_roof(session, product_code="HOUSE_B")
    assert failed_job is not None
    lifecycle.start_pre_roof_inspection(production_job_id=failed_job.job_id)
    lifecycle.fail_pre_roof_inspection(production_job_id=failed_job.job_id, reason="structure check failed")
    stored_failed = session.get(type(failed_job), failed_job.job_id)
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == failed_job.job_id))
    assert stored_failed is not None and stored_failed.status is JobStatus.PRE_ROOF_READY
    assert inspection is not None and inspection.status is ProductionInspectionStatus.COMPLETED
    assert inspection.result is ProductionInspectionResultCode.FAIL
    assert inspection.production_valid is False
    assert inspection.is_passed is False
    assert _roof_step(session, failed_job.job_id) is None
    assert session.scalar(select(func.count()).select_from(ProductionEvent).where(
        ProductionEvent.job_id == failed_job.job_id,
        ProductionEvent.event_type == EventType.JOB_FAILED,
    )) == 0
    with pytest.raises(InvalidProductionCompletionTransitionError):
        lifecycle.pass_pre_roof_inspection(production_job_id=failed_job.job_id)


def test_pre_roof_reinspection_preserves_failed_cycles_and_materializes_roof_once(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    assert job is not None
    lifecycle = ProductionCompletionService(session)

    cycle1 = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert cycle1.inspection_cycle == 1
    lifecycle.fail_pre_roof_inspection(production_job_id=job.job_id, reason="cycle one failed")

    cycle2 = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert cycle2.inspection_cycle == 2
    assert cycle2.status is ProductionInspectionStatus.RUNNING
    with pytest.raises(InvalidProductionCompletionTransitionError):
        lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    lifecycle.fail_pre_roof_inspection(production_job_id=job.job_id, reason="cycle two failed")

    cycle3 = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert cycle3.inspection_cycle == 3
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)

    inspections = list(session.scalars(
        select(ProductionInspection)
        .where(ProductionInspection.production_job_id == job.job_id)
        .order_by(ProductionInspection.inspection_cycle)
    ))
    assert [(row.inspection_cycle, row.status, row.result, row.failure_reason) for row in inspections] == [
        (1, ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.FAIL, "cycle one failed"),
        (2, ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.FAIL, "cycle two failed"),
        (3, ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.PASS, None),
    ]
    latest = inspections[-1]
    assert (latest.status, latest.result, latest.production_valid) == (
        ProductionInspectionStatus.COMPLETED,
        ProductionInspectionResultCode.PASS,
        True,
    )
    assert session.get(ProductionJob, job.job_id).status is JobStatus.ROOF_READY
    assert _roof_step(session, job.job_id) is roof_step
    assert session.scalar(select(func.count()).select_from(JobStep).where(
        JobStep.job_id == job.job_id,
        JobStep.operation_code == "INSTALL_ROOF",
    )) == 1
    with pytest.raises(InvalidProductionCompletionTransitionError):
        lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    with pytest.raises(InvalidProductionCompletionTransitionError):
        lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)


def test_duplicate_pre_roof_fail_does_not_reset_or_create_history(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    assert job is not None
    lifecycle = ProductionCompletionService(session)

    cycle1 = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    lifecycle.fail_pre_roof_inspection(production_job_id=job.job_id, reason="first failure")
    with pytest.raises(InvalidProductionCompletionTransitionError):
        lifecycle.fail_pre_roof_inspection(production_job_id=job.job_id, reason="duplicate failure")

    inspections = list(session.scalars(
        select(ProductionInspection)
        .where(ProductionInspection.production_job_id == job.job_id)
        .order_by(ProductionInspection.inspection_cycle)
    ))
    assert [(row.inspection_cycle, row.status, row.result, row.failure_reason) for row in inspections] == [
        (cycle1.inspection_cycle, ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.FAIL, "first failure"),
    ]
    assert session.get(ProductionJob, job.job_id).status is JobStatus.PRE_ROOF_READY
    assert _roof_step(session, job.job_id) is None


def test_roof_step_failure_mismatch_and_duplicate_completion_are_safe(session: Session) -> None:
    job = _job_at_pre_roof(session, roof=RoofOptionCode.ROOF_01)
    assert job is not None
    roof_step = _pass_inspection(session, job.job_id)
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_step(roof_step.job_step_id)
    orchestration.fail_step(roof_step.job_step_id, reason="roof fixture failure")
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.FAILED
    assert session.get(type(job), job.job_id).status is JobStatus.FAILED
    assert session.get(type(job), job.job_id).completed_at is None

    complete_job = _job_at_pre_roof(session, roof=RoofOptionCode.ROOF_02)
    assert complete_job is not None
    complete_step = _pass_inspection(session, complete_job.job_id)
    orchestration.start_step(complete_step.job_step_id)
    orchestration.complete_step(complete_step.job_step_id)
    assert session.get(type(complete_job), complete_job.job_id).status is JobStatus.ROOF_READY
    orchestration.complete_job(complete_job.job_id)
    completed_at = session.get(type(complete_job), complete_job.job_id).completed_at
    with pytest.raises(InvalidProductionStateTransitionError):
        orchestration.complete_step(complete_step.job_step_id)
    assert session.get(type(complete_job), complete_job.job_id).completed_at == completed_at.replace(tzinfo=None)


def test_inspection_pass_and_roof_complete_roll_back_without_partial_state(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job_at_pre_roof(session)
    assert job is not None
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    monkeypatch.setattr(session, "flush", lambda: (_ for _ in ()).throw(RuntimeError("roof step flush failure")))
    with pytest.raises(RuntimeError, match="roof step flush failure"):
        lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    session.expire_all()
    assert session.get(type(job), job.job_id).status is JobStatus.PRE_ROOF_READY
    assert session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id)).status is ProductionInspectionStatus.RUNNING
    assert _roof_step(session, job.job_id) is None

    monkeypatch.undo()
    roof_step = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_step(roof_step.job_step_id)
    monkeypatch.setattr(orchestration, "_record_event", lambda **_: (_ for _ in ()).throw(RuntimeError("completion event failure")))
    with pytest.raises(RuntimeError, match="completion event failure"):
        orchestration.complete_step(roof_step.job_step_id)
    session.expire_all()
    assert session.get(type(job), job.job_id).status is JobStatus.ROOF_READY
    assert session.get(type(job), job.job_id).completed_at is None
    assert session.get(JobStep, roof_step.job_step_id).status is StepStatus.RUNNING


def test_two_recipe_jobs_have_independent_runtime_roof_steps(session: Session) -> None:
    first = _job_at_pre_roof(session, roof=RoofOptionCode.ROOF_02)
    second = _create_recipe_job(session, roof=RoofOptionCode.ROOF_02)
    assert first is not None
    _complete_assembly(session, second.job_id, count=10)
    roof_step = _pass_inspection(session, first.job_id)
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_step(roof_step.job_step_id)
    orchestration.complete_step(roof_step.job_step_id)
    assert session.get(type(first), first.job_id).status is JobStatus.ROOF_READY
    orchestration.complete_job(first.job_id)
    assert session.get(type(first), first.job_id).status is JobStatus.COMPLETED
    assert session.get(type(second), second.job_id).status is JobStatus.RUNNING



def test_execution_gate_delays_only_pre_roof_stage_and_snapshots_manual_roof_delivery(session: Session) -> None:
    """A seven-stage master yields six initial steps; Roof arrives only after PASS."""
    product = Product(product_code="TEST_GATE_SEVEN", product_name="Synthetic gated recipe")
    normal_part = Part(
        part_code="TEST_GATE_NORMAL_PART", part_name="Synthetic normal", category=PartCategory.STRUCTURE,
        vision_class="wall_ext_back", unit="EA",
    )
    roof_part = session.get(Part, "ROOF_01") or Part(
        part_code="ROOF_01", part_name="Synthetic roof", category=PartCategory.STRUCTURE,
        vision_class="roof_01", unit="EA",
    )
    session.add_all([product, normal_part, roof_part])
    session.flush()
    session.add(Inventory(part_code=normal_part.part_code, quantity=10))
    if session.get(Inventory, roof_part.part_code) is None:
        session.add(Inventory(part_code=roof_part.part_code, quantity=10))
    session.add(InstallationSlot(
        product_code=product.product_code,
        slot_code="TEST_GATE_ROOF_SLOT",
        display_name="Synthetic gated Roof slot",
    ))
    session.flush()
    recipe = AssemblyRecipe(product_code=product.product_code, version=1, is_active=True)
    session.add(recipe)
    session.flush()
    session.add_all(
        AssemblyRecipeStage(
            recipe_id=recipe.recipe_id,
            stage_order=order,
            operation_code=f"TEST_INITIAL_{order}",
            display_name=f"Initial {order}",
            part_code=normal_part.part_code if order == 1 else None,
            quantity=1 if order == 1 else None,
        )
        for order in range(1, 7)
    )
    roof_stage = AssemblyRecipeStage(
        recipe_id=recipe.recipe_id, stage_order=7, operation_code="INSTALL_ROOF",
        display_name="Synthetic delayed roof", part_code="ROOF_01", quantity=1,
        slot_code="TEST_GATE_ROOF_SLOT", pick_zone="TEST_GATE_ROOF_ZONE", option_code="ROOF_01",
        execution_gate="PRE_ROOF_PASS", supply_mode=SupplyMode.MANUAL,
        supply_group_code="TEST_GATE_ROOF_GROUP",
        is_terminal=True,
    )
    session.add(roof_stage)
    session.commit()

    job = ProductionOrchestrationService(session).create_job(
        product_code=product.product_code, job_code="TEST-GATE-SEVEN", roof_option_code=RoofOptionCode.ROOF_01,
    )
    initial_steps = list(session.scalars(select(JobStep).where(JobStep.job_id == job.job_id)))
    assert session.scalar(select(func.count()).select_from(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)) == 7
    assert len(initial_steps) == 6
    assert not any(step.operation_code == "INSTALL_ROOF" for step in initial_steps)
    delivery = session.scalar(
        select(JobMaterialDelivery).where(
            JobMaterialDelivery.production_job_id == job.job_id,
            JobMaterialDelivery.supply_group_code == "TEST_GATE_ROOF_GROUP",
        )
    )
    assert delivery is not None
    deferred_item = session.scalar(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == delivery.job_delivery_id
    ))
    assert deferred_item is not None and deferred_item.job_step_id is None

    _complete_assembly(session, job.job_id, count=6)
    roof_step = _pass_inspection(session, job.job_id)
    delivery = session.scalar(
        select(JobMaterialDelivery).where(
            JobMaterialDelivery.production_job_id == job.job_id,
            JobMaterialDelivery.supply_group_code == "TEST_GATE_ROOF_GROUP",
        )
    )
    assert roof_step.source_recipe_stage_id == roof_stage.recipe_stage_id
    assert (roof_step.part_code, roof_step.vision_class, roof_step.quantity, roof_step.slot_code, roof_step.pick_zone) == (
        "ROOF_01", "roof_01", 1, "TEST_GATE_ROOF_SLOT", "TEST_GATE_ROOF_ZONE"
    )
    assert roof_step.supply_mode is SupplyMode.MANUAL
    assert delivery is not None and delivery.supply_mode is SupplyMode.MANUAL
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id == delivery.job_delivery_id)) == 1
    session.refresh(deferred_item)
    assert deferred_item.job_step_id == roof_step.job_step_id


def test_unknown_execution_gate_fails_before_job_materialization(session: Session) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None
    stage = session.scalar(select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id, AssemblyRecipeStage.stage_order == 1))
    assert stage is not None
    stage.execution_gate = "UNAPPROVED_GATE"
    session.commit()

    with pytest.raises(UnsupportedExecutionGateError):
        ProductionOrchestrationService(session).create_job(
            product_code="HOUSE_A", job_code="UNKNOWN-GATE-REJECT", roof_option_code=RoofOptionCode.ROOF_01,
        )
    assert session.scalar(select(func.count()).select_from(ProductionJob).where(ProductionJob.job_code == "UNKNOWN-GATE-REJECT")) == 0


def test_pre_roof_pass_uses_job_recipe_not_later_active_recipe(session: Session) -> None:
    v1 = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert v1 is not None
    job = _create_recipe_job(session, product_code="HOUSE_A", roof=RoofOptionCode.ROOF_02)
    _complete_assembly(session, job.job_id)
    v1_roof = session.scalar(select(AssemblyRecipeStage).where(
        AssemblyRecipeStage.recipe_id == v1.recipe_id,
        AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
        AssemblyRecipeStage.option_code == "ROOF_02",
    ))
    assert v1_roof is not None

    v1.is_active = False
    session.flush()
    v2 = add_active_recipe(session, "HOUSE_A", version=2, stages=HOUSE_A_STAGES)
    add_gated_roof_stages(session, v2)
    session.commit()
    assert v2.recipe_id != v1.recipe_id

    roof_step = _pass_inspection(session, job.job_id)
    assert roof_step.source_recipe_stage_id == v1_roof.recipe_stage_id
    assert roof_step.source_recipe_stage_id != session.scalar(select(AssemblyRecipeStage.recipe_stage_id).where(
        AssemblyRecipeStage.recipe_id == v2.recipe_id,
        AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
        AssemblyRecipeStage.option_code == "ROOF_02",
    ))


def test_roof_stage_selection_failure_rolls_back_pre_roof_pass(session: Session) -> None:
    job = _job_at_pre_roof(session, roof=RoofOptionCode.ROOF_01)
    assert job is not None
    recipe = session.get(AssemblyRecipe, job.assembly_recipe_id)
    assert recipe is not None
    original = session.scalar(select(AssemblyRecipeStage).where(
        AssemblyRecipeStage.recipe_id == recipe.recipe_id,
        AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
        AssemblyRecipeStage.option_code == "ROOF_01",
    ))
    assert original is not None
    session.add(AssemblyRecipeStage(
        recipe_id=recipe.recipe_id, stage_order=99, operation_code="INSTALL_ROOF", display_name="Duplicate",
        part_code=original.part_code, quantity=1, slot_code=original.slot_code, pick_zone=original.pick_zone,
        option_code="ROOF_01", execution_gate="PRE_ROOF_PASS", supply_mode=SupplyMode.MANUAL,
        supply_group_code="TEST_DUPLICATE_ROOF_GROUP",
    ))
    session.commit()
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    with pytest.raises(RoofStageMaterializationError, match="exactly one"):
        lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    session.expire_all()
    assert session.get(ProductionJob, job.job_id).status is JobStatus.PRE_ROOF_READY
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.RUNNING
    assert _roof_step(session, job.job_id) is None



def test_pre_roof_start_has_immutable_request_identity_and_nonterminal_invariants(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)

    assert inspection.inspection_cycle == 1
    assert inspection.status is ProductionInspectionStatus.RUNNING
    assert inspection.result is None
    assert inspection.production_valid is False
    assert inspection.vision_production_valid is False
    assert len(inspection.inspection_request_id) == 36


def test_not_evaluated_and_error_are_terminal_without_opening_roof_and_can_reinspect(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    lifecycle = ProductionCompletionService(session)
    first = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    not_evaluated = lifecycle.not_evaluated_pre_roof_inspection(production_job_id=job.job_id, reason="camera unavailable")
    assert (not_evaluated.status, not_evaluated.result, not_evaluated.production_valid, not_evaluated.is_passed) == (
        ProductionInspectionStatus.COMPLETED, ProductionInspectionResultCode.NOT_EVALUATED, False, None,
    )
    assert _roof_step(session, job.job_id) is None

    second = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert second.inspection_cycle == 2
    assert second.inspection_request_id != first.inspection_request_id
    errored = lifecycle.error_pre_roof_inspection(production_job_id=job.job_id, reason="transport unavailable")
    assert (errored.status, errored.result, errored.production_valid, errored.is_passed) == (
        ProductionInspectionStatus.ERROR, None, False, None,
    )
    third = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    assert third.inspection_cycle == 3
    assert third.inspection_request_id not in {first.inspection_request_id, second.inspection_request_id}
    assert _roof_step(session, job.job_id) is None


def test_pass_without_server_production_valid_opens_pre_roof_gate_v02(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    lifecycle = ProductionCompletionService(session)
    inspection = lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    inspection.status = ProductionInspectionStatus.COMPLETED
    inspection.result = ProductionInspectionResultCode.PASS
    inspection.production_valid = False
    inspection.is_passed = True
    session.commit()

    assert lifecycle.is_pre_roof_gate_open(inspection) is True
    # The row was deliberately mutated only for this gate predicate test; it
    # does not simulate the v0.2 result handler that materializes the roof.
    assert _roof_step(session, job.job_id) is None


def test_view_results_preserve_pass_fail_and_not_evaluated(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
    session.add_all([
        ProductionInspectionResult(inspection_id=inspection.inspection_id, item_code="VIEW_TOP", item_name="Top", result=ProductionInspectionResultCode.PASS, is_passed=True),
        ProductionInspectionResult(inspection_id=inspection.inspection_id, item_code="VIEW_LEFT", item_name="Left", result=ProductionInspectionResultCode.FAIL, is_passed=False),
        ProductionInspectionResult(inspection_id=inspection.inspection_id, item_code="VIEW_RIGHT", item_name="Right", result=ProductionInspectionResultCode.NOT_EVALUATED, is_passed=None),
    ])
    session.commit()
    assert [result.result for result in session.scalars(select(ProductionInspectionResult).order_by(ProductionInspectionResult.result_id))] == [
        ProductionInspectionResultCode.PASS,
        ProductionInspectionResultCode.FAIL,
        ProductionInspectionResultCode.NOT_EVALUATED,
    ]



def test_persisted_pre_roof_request_id_is_immutable(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)

    with pytest.raises(ValueError, match="immutable"):
        inspection.inspection_request_id = str(uuid.uuid4())



def test_fail_or_not_evaluated_can_never_be_server_production_valid(session: Session) -> None:
    job = _job_at_pre_roof(session, product_code="HOUSE_B")
    inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
    inspection.status = ProductionInspectionStatus.COMPLETED
    inspection.result = ProductionInspectionResultCode.FAIL
    inspection.production_valid = True
    inspection.is_passed = False

    with pytest.raises(IntegrityError):
        session.commit()
