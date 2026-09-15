from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage, Product
from shared.services.assembly_recipe_service import (
    ActiveAssemblyRecipeNotFoundError,
    AssemblyRecipeService,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import HOUSE_A_STAGES, HOUSE_B_STAGES, add_active_recipe, add_material_requirement


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all(
        [
            Product(product_code="HOUSE_A", product_name="A형 주택"),
            Product(product_code="HOUSE_B", product_name="B형 주택"),
            Product(product_code="HOUSE_EMPTY", product_name="Recipe 없는 주택"),
        ]
    )
    db.flush()
    add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES)
    add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES)
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _stage_values(recipe: AssemblyRecipe) -> list[tuple[int, str, str]]:
    return [
        (stage.stage_order, stage.operation_code, stage.display_name)
        for stage in AssemblyRecipeService.get_ordered_stages(recipe)
    ]


def test_house_a_and_house_b_v1_recipes_match_the_current_pre_roof_plan(session: Session) -> None:
    recipes = AssemblyRecipeService(session)

    house_a = recipes.get_active_recipe_for_product("HOUSE_A")
    house_b = recipes.get_active_recipe_for_product("HOUSE_B")

    assert house_a.version == house_b.version == 1
    assert _stage_values(house_a) == HOUSE_A_STAGES
    assert _stage_values(house_b) == HOUSE_B_STAGES
    assert house_a.stages[3].operation_code == "INSTALL_KITCHEN_SINK"
    assert house_b.stages[3].operation_code == "INSTALL_BATHTUB"


def test_missing_active_recipe_blocks_new_job_creation(session: Session) -> None:
    product = session.query(Product).filter_by(product_code="HOUSE_EMPTY").one()

    with pytest.raises(ActiveAssemblyRecipeNotFoundError):
        ProductionOrchestrationService(session).create_job(
            product_code=product.product_code,
            job_code="RECIPE-MISSING-001",
        )


def test_active_recipe_partial_unique_constraint_rejects_two_active_versions(session: Session) -> None:
    session.add(
        AssemblyRecipe(
            product_code="HOUSE_A",
            version=2,
            is_active=True,
            description="conflicting active version",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_job_keeps_recipe_v1_and_snapshot_after_v2_becomes_active(session: Session) -> None:
    product = session.query(Product).filter_by(product_code="HOUSE_A").one()
    service = ProductionOrchestrationService(session)
    first_job = service.create_job(product_code=product.product_code, job_code="RECIPE-V1-001")
    first_recipe_id = first_job.assembly_recipe_id
    first_step_codes = [step.operation_code for step in first_job.steps]

    v1 = session.get(AssemblyRecipe, first_recipe_id)
    assert v1 is not None
    v1.is_active = False
    v2_stages = list(HOUSE_A_STAGES)
    v2_stages[5], v2_stages[6] = v2_stages[6], v2_stages[5]
    v2_stages = [
        (order, operation, name)
        for order, (_, operation, name) in enumerate(v2_stages, start=1)
    ]
    v2 = AssemblyRecipe(
        product_code="HOUSE_A",
        version=2,
        is_active=True,
        description="v2 test order",
    )
    session.add(v2)
    session.flush()
    session.add_all(
        AssemblyRecipeStage(
            recipe_id=v2.recipe_id,
            stage_order=order,
            operation_code=operation,
            display_name=name,
            is_terminal=order == max(item[0] for item in v2_stages),
        )
        for order, operation, name in v2_stages
    )
    session.flush()
    add_material_requirement(session, v2, inventory_quantity=100)
    session.commit()

    second_job = service.create_job(product_code=product.product_code, job_code="RECIPE-V2-001")

    assert first_job.assembly_recipe_id == first_recipe_id
    assert [step.operation_code for step in first_job.steps] == first_step_codes
    assert second_job.assembly_recipe_id == v2.recipe_id
    assert [step.operation_code for step in second_job.steps][5:7] == [
        "INSTALL_WASHING_MACHINE",
        "INSTALL_REFRIGERATOR",
    ]
