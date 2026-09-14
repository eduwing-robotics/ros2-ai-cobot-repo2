import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy import text

from shared.models import Base
from shared.models.factory import (
    Product,
    Part,
    PartCategory,
    Inventory,
    AssemblyRecipe,
    AssemblyRecipeStage,
    RoofOptionCode,
)
from shared.services.production_inventory_preflight_service import (
    ProductionInventoryPreflightService,
    ProductionInventoryPreflightResult,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def preflight_session(session: Session) -> Session:
    product = Product(product_code="TEST_PROD", product_name="Test Product")
    session.add(product)

    parts = [
        Part(part_code="PART_A", part_name="Part A", category=PartCategory.STRUCTURE, unit="ea"),
        Part(part_code="PART_B", part_name="Part B", category=PartCategory.STRUCTURE, unit="ea"),
        Part(part_code="ROOF_1", part_name="Roof 1", category=PartCategory.STRUCTURE, unit="ea"),
        Part(part_code="ROOF_2", part_name="Roof 2", category=PartCategory.STRUCTURE, unit="ea"),
        Part(part_code="INACTIVE_PART", part_name="Inactive Part", category=PartCategory.STRUCTURE, unit="ea", is_active=False),
    ]
    session.add_all(parts)

    recipe = AssemblyRecipe(product_code="TEST_PROD", version=1, is_active=True)
    session.add(recipe)
    session.flush()

    stages = [
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=1, operation_code="OP1", display_name="OP 1", part_code="PART_A", quantity=2),
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=2, operation_code="OP2", display_name="OP 2", part_code="PART_B", quantity=1),
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=3, operation_code="OP3", display_name="OP 3", part_code="PART_A", quantity=3), # duplicate part_code test
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=4, operation_code="OP4", display_name="OP 4", part_code=None, quantity=None), # operation-only stage
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=5, operation_code="OP5", display_name="OP 5", part_code="ROOF_1", quantity=1, option_code="ROOF_01"),
        AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=6, operation_code="OP6", display_name="OP 6", part_code="ROOF_2", quantity=1, option_code="ROOF_02"),
    ]
    session.add_all(stages)
    session.commit()
    return session


def set_inventory(session: Session, part_code: str, quantity: int) -> None:
    inv = session.get(Inventory, part_code)
    if inv:
        inv.quantity = quantity
    else:
        session.add(Inventory(part_code=part_code, quantity=quantity))
    session.commit()


def test_r1_r2_r4_requirement_calculation_and_p1_exact_quantity(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 5) # 2 + 3
    set_inventory(preflight_session, "PART_B", 1)

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)

    assert result.can_produce is True
    assert len(result.shortages) == 0


def test_p2_more_quantity_passes(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 10)
    set_inventory(preflight_session, "PART_B", 5)

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)
    assert result.can_produce is True


def test_p3_one_part_shortage(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 4) # Required 5, shortage 1
    set_inventory(preflight_session, "PART_B", 1)

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)

    assert result.can_produce is False
    assert len(result.shortages) == 1
    assert result.shortages[0].part_code == "PART_A"
    assert result.shortages[0].shortage_quantity == 1


def test_p4_multiple_part_shortages(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 3) # short 2
    set_inventory(preflight_session, "PART_B", 0) # short 1

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)

    assert result.can_produce is False
    assert len(result.shortages) == 2
    codes = {s.part_code: s.shortage_quantity for s in result.shortages}
    assert codes["PART_A"] == 2
    assert codes["PART_B"] == 1


def test_p5_missing_inventory_row(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_B", 1)
    # PART_A inventory row is missing completely
    preflight_session.execute(text("DELETE FROM inventory WHERE part_code='PART_A'"))

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)

    assert result.can_produce is False
    assert len(result.shortages) == 1
    assert result.shortages[0].part_code == "PART_A"
    assert result.shortages[0].available_quantity == 0
    assert result.shortages[0].shortage_quantity == 5


def test_r3_p6_quantity_multiplier(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 9) # Required 5 * 2 = 10, short 1
    set_inventory(preflight_session, "PART_B", 2) # Required 1 * 2 = 2

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=2)

    assert result.can_produce is False
    assert len(result.shortages) == 1
    assert result.shortages[0].part_code == "PART_A"
    assert result.shortages[0].shortage_quantity == 1
    assert result.shortages[0].required_quantity == 10


def test_r5_p7_roof_option_handling(preflight_session: Session) -> None:
    set_inventory(preflight_session, "PART_A", 5)
    set_inventory(preflight_session, "PART_B", 1)
    set_inventory(preflight_session, "ROOF_1", 1)
    set_inventory(preflight_session, "ROOF_2", 0)

    svc = ProductionInventoryPreflightService(preflight_session)

    # Roof 01 passes
    r1 = svc.validate(product_code="TEST_PROD", quantity=1, roof_option_code=RoofOptionCode.ROOF_01)
    assert r1.can_produce is True

    # Roof 02 fails because ROOF_2 qty is 0
    r2 = svc.validate(product_code="TEST_PROD", quantity=1, roof_option_code=RoofOptionCode.ROOF_02)
    assert r2.can_produce is False
    assert len(r2.shortages) == 1
    assert r2.shortages[0].part_code == "ROOF_2"


def test_p8_inactive_part_fails(preflight_session: Session) -> None:
    # Add an inactive part requirement
    recipe = preflight_session.execute(text("SELECT recipe_id FROM assembly_recipes WHERE product_code='TEST_PROD'")).scalar()
    preflight_session.add(AssemblyRecipeStage(
        recipe_id=recipe, stage_order=10, operation_code="OP10", display_name="OP 10",
        part_code="INACTIVE_PART", quantity=1
    ))
    preflight_session.commit()

    set_inventory(preflight_session, "PART_A", 5)
    set_inventory(preflight_session, "PART_B", 1)
    set_inventory(preflight_session, "INACTIVE_PART", 10) # Enough qty but inactive

    svc = ProductionInventoryPreflightService(preflight_session)
    result = svc.validate(product_code="TEST_PROD", quantity=1)

    assert result.can_produce is False
    assert len(result.shortages) == 1
    assert result.shortages[0].part_code == "INACTIVE_PART"
    assert result.shortages[0].available_quantity == 0 # Force treated as 0
