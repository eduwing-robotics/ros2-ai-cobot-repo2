from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from scripts.seed_house_a_mvp_master import (
    HOUSE_A_PARTS,
    HOUSE_A_PRODUCT_CODE,
    HOUSE_A_SLOTS,
    HOUSE_A_STAGES,
    seed_house_a_current_master,
)
from shared.models import Base
from shared.schemas.vision import IncomingQARequestV02
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    IncomingQATransaction,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    InstallationSlot,
    Part,
    RoofOptionCode,
    SupplyMode,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import seed_inventory_for_recipe


EXPECTED_VISION_CLASSES = {
    "BASE-HOUSE-A-01": "base_house_a",
    "WALL-EXT-BACK-WINDOW-01": "wall_ext_back_window",
    "WALL-EXT-DOOR-01": "wall_ext_door",
    "WALL-EXT-LEFT-WINDOW-01": "wall_ext_left_window",
    "WALL-EXT-RIGHT-01": "wall_ext_right",
    "WALL-INT-HOUSE-A-01": "wall_int_house_a",
    "WALL-INT-HOUSE-A-DOOR-01": "wall_int_house_a_door",
    "ROOF-NOZIP-01": "roof_nozip",
}


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        yield db
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(session: Session) -> AssemblyRecipe:
    recipe = seed_house_a_current_master(session)
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return recipe


def _create_job(session: Session):
    _seed(session)
    return ProductionOrchestrationService(session).create_job(
        product_code=HOUSE_A_PRODUCT_CODE,
        job_code="HOUSE-A-CURRENT-TEST-001",
        roof_option_code=RoofOptionCode.ROOF_01,
    )


def test_house_a_current_master_is_versioned_idempotent_and_has_no_legacy_steps(session: Session) -> None:
    first = _seed(session)
    second = seed_house_a_current_master(session)
    session.commit()

    assert first.recipe_id == second.recipe_id
    assert first.is_active is True
    assert session.scalar(
        select(func.count()).select_from(Part).where(Part.part_code.in_(EXPECTED_VISION_CLASSES))
    ) == 8
    assert dict(session.execute(
        select(Part.part_code, Part.vision_class).where(Part.part_code.in_(EXPECTED_VISION_CLASSES))
    ).all()) == EXPECTED_VISION_CLASSES
    assert {row[0] for row in session.execute(
        select(InstallationSlot.slot_code)
    )} == {row[0] for row in HOUSE_A_SLOTS}

    stages = list(session.scalars(
        select(AssemblyRecipeStage)
        .where(AssemblyRecipeStage.recipe_id == first.recipe_id)
        .order_by(AssemblyRecipeStage.stage_order)
    ))
    assert [(stage.stage_order, stage.operation_code, stage.part_code) for stage in stages] == [
        (row[0], row[1], row[3]) for row in HOUSE_A_STAGES
    ]
    assert [stage.operation_code for stage in stages[1:5]] == [
        "INSTALL_DOOR_OUTER_WALL",
        "INSTALL_LEFT_OUTER_WALL",
        "INSTALL_REAR_OUTER_WALL",
        "INSTALL_RIGHT_OUTER_WALL",
    ]
    assert [stage.part_code for stage in stages[5:7]] == [
        "WALL-INT-HOUSE-A-DOOR-01", "WALL-INT-HOUSE-A-01"
    ]
    assert all("TOILET" not in stage.operation_code for stage in stages)
    assert all("BASIN" not in stage.operation_code for stage in stages)
    assert all("KITCHEN" not in stage.operation_code for stage in stages)
    assert all("COOKTOP" not in stage.operation_code for stage in stages)
    assert all("REFRIGERATOR" not in stage.operation_code for stage in stages)
    assert all("WASHING_MACHINE" not in stage.operation_code for stage in stages)
    assert all("INSTALL_WINDOW" not in stage.operation_code for stage in stages)
    assert all(stage.part_code != "TEST_MATERIAL_HOUSE_A_1" for stage in stages)
    roof = stages[-1]
    assert roof.execution_gate == "PRE_ROOF_PASS"
    assert roof.option_code == RoofOptionCode.ROOF_01.value
    assert roof.is_terminal is True


def test_house_a_new_job_uses_current_recipe_with_four_outer_and_two_inner_steps(session: Session) -> None:
    job = _create_job(session)
    steps = list(session.scalars(
        select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)
    ))
    assert job.assembly_recipe_id is not None
    assert len(steps) == 7  # PRE_ROOF-gated roof is materialized only after PASS.
    assert [step.part_code for step in steps] == [row[3] for row in HOUSE_A_STAGES[:7]]
    assert len([step for step in steps if step.operation_code == "INSTALL_INNER_WALL"]) == 2
    assert not any("FURNITURE" in step.operation_code or "WINDOW" in step.operation_code for step in steps)

    deliveries = list(session.scalars(
        select(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job.job_id)
        .order_by(JobMaterialDelivery.batch_order)
    ))
    by_group = {delivery.supply_group_code: delivery for delivery in deliveries}
    assert set(by_group) == {"BASE", "OUTER_WALLS", "INNER_WALL", "ROOF"}
    assert by_group["OUTER_WALLS"].supply_mode is SupplyMode.TRANSPORTED
    assert by_group["INNER_WALL"].supply_mode is SupplyMode.TRANSPORTED
    inner_items = list(session.scalars(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == by_group["INNER_WALL"].job_delivery_id
    )))
    assert {(item.part_code, item.quantity) for item in inner_items} == {
        ("WALL-INT-HOUSE-A-01", 1), ("WALL-INT-HOUSE-A-DOOR-01", 1)
    }


def test_house_a_generated_v02_house_items_match_a01_to_a07_without_legacy_materials(session: Session) -> None:
    job = _create_job(session)
    service = IncomingQAV02OrchestrationService(session)
    base = service.complete_current_mode_for_test_override(job_id=job.job_id)
    assert base.inspection_mode == "BASE_AB"
    followup = service.reconcile_job(job_id=job.job_id)
    assert followup.created_transaction_ids
    house = session.get(IncomingQATransaction, followup.created_transaction_ids[0])
    assert house is not None and house.inspection_mode == "HOUSE_A"
    request = IncomingQARequestV02.model_validate_json(house.immutable_request_snapshot)
    assert {item.slot_id: item.expected_part_code for item in request.items} == {
        "A01": "WALL-EXT-BACK-WINDOW-01",
        "A02": "WALL-EXT-DOOR-01",
        "A03": "WALL-EXT-LEFT-WINDOW-01",
        "A04": "WALL-EXT-RIGHT-01",
        "A05": "WALL-INT-HOUSE-A-01",
        "A06": "ROOF-NOZIP-01",
        "A07": "WALL-INT-HOUSE-A-DOOR-01",
    }
    items = list(house.inspections)
    assert {item.expected_part_code for item in items} == {
        "WALL-EXT-BACK-WINDOW-01",
        "WALL-EXT-DOOR-01",
        "WALL-EXT-LEFT-WINDOW-01",
        "WALL-EXT-RIGHT-01",
        "WALL-INT-HOUSE-A-01",
        "WALL-INT-HOUSE-A-DOOR-01",
        "ROOF-NOZIP-01",
    }
    assert {item.expected_class_name for item in items} == {
        "wall_ext_back_window",
        "wall_ext_door",
        "wall_ext_left_window",
        "wall_ext_right",
        "wall_int_house_a",
        "wall_int_house_a_door",
        "roof_nozip",
    }
    assert all(item.expected_part_code != "TEST_MATERIAL_HOUSE_A_1" for item in items)
