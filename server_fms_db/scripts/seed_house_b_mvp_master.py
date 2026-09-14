"""Idempotently seed the approved HOUSE_B MVP production master.

The seed intentionally contains master/recipe data only.  Operational inventory
is not seeded here.  The CLI refuses to run unless its target is the dedicated
``smart_factory_benchmark`` PostgreSQL database and distinct from DATABASE_URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    InstallationSlot,
    Part,
    PartCategory,
    Product,
    RoofOptionCode,
    SupplyMode,
)

HOUSE_B_PRODUCT_CODE = "HOUSE_B"
HOUSE_B_RECIPE_DESCRIPTION = "HOUSE_B MVP: base, inner wall, four outer walls, gated sloped roof"

HOUSE_B_PARTS = (
    ("BASE-HOUSE-B-01", "밑판", "base_house_b"),
    ("WALL-INT-HOUSE-B-01", "내벽", "wall_int_house_b"),
    ("WALL-EXT-BACK-WINDOW-01", "후면 창문 결합 외벽", "wall_ext_back_window"),
    ("WALL-EXT-DOOR-01", "출입문 결합 외벽", "wall_ext_door"),
    ("WALL-EXT-LEFT-WINDOW-01", "좌측 창문 결합 외벽", "wall_ext_left_window"),
    ("WALL-EXT-RIGHT-01", "우측 외벽", "wall_ext_right"),
    ("ROOF-ZIP-01", "경사지붕", "roof_zip"),
)

HOUSE_B_SLOTS = (
    ("HOUSE_B_FLOOR_01", "HOUSE_B 밑판 설치 위치"),
    ("HOUSE_B_INNER_WALL_01", "HOUSE_B 내벽 설치 위치"),
    ("HOUSE_B_OUTER_WALL_REAR_01", "HOUSE_B 후면 외벽 설치 위치"),
    ("HOUSE_B_OUTER_WALL_DOOR_01", "HOUSE_B 출입문 외벽 설치 위치"),
    ("HOUSE_B_OUTER_WALL_LEFT_01", "HOUSE_B 좌측 외벽 설치 위치"),
    ("HOUSE_B_OUTER_WALL_RIGHT_01", "HOUSE_B 우측 외벽 설치 위치"),
    ("HOUSE_B_ROOF_01", "HOUSE_B 지붕 설치 위치"),
)

# (order, operation, display, part, slot, zone, option, gate, mode, group, destination)
HOUSE_B_STAGES = (
    (1, "INSTALL_BASE", "밑판 설치", "BASE-HOUSE-B-01", "HOUSE_B_FLOOR_01", None, None, None, SupplyMode.MANUAL, "BASE", None, False),
    (2, "INSTALL_DOOR_OUTER_WALL", "출입문 외벽 설치", "WALL-EXT-DOOR-01", "HOUSE_B_OUTER_WALL_DOOR_01", "WALL_EXT_PALLET", None, None, SupplyMode.TRANSPORTED, "OUTER_WALLS", "DROP", False),
    (3, "INSTALL_LEFT_OUTER_WALL", "좌측 창문 외벽 설치", "WALL-EXT-LEFT-WINDOW-01", "HOUSE_B_OUTER_WALL_LEFT_01", "WALL_EXT_PALLET", None, None, SupplyMode.TRANSPORTED, "OUTER_WALLS", "DROP", False),
    (4, "INSTALL_REAR_OUTER_WALL", "후면 창문 외벽 설치", "WALL-EXT-BACK-WINDOW-01", "HOUSE_B_OUTER_WALL_REAR_01", "WALL_EXT_PALLET", None, None, SupplyMode.TRANSPORTED, "OUTER_WALLS", "DROP", False),
    (5, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치", "WALL-EXT-RIGHT-01", "HOUSE_B_OUTER_WALL_RIGHT_01", "WALL_EXT_PALLET", None, None, SupplyMode.TRANSPORTED, "OUTER_WALLS", "DROP", False),
    (6, "INSTALL_INNER_WALL", "내벽 설치", "WALL-INT-HOUSE-B-01", "HOUSE_B_INNER_WALL_01", "WALL_INT_PALLET", None, None, SupplyMode.TRANSPORTED, "INNER_WALL", "DROP", False),
    (7, "INSTALL_ROOF", "경사지붕 설치", "ROOF-ZIP-01", "HOUSE_B_ROOF_01", None, RoofOptionCode.ROOF_02.value, "PRE_ROOF_PASS", SupplyMode.MANUAL, "ROOF", None, True),
)


def _conflict(entity: str, key: str, field: str) -> RuntimeError:
    return RuntimeError(f"HOUSE_B MVP seed conflict: {entity} {key!r} has incompatible {field}.")


def _seed_product(session: Session) -> Product:
    product = session.get(Product, HOUSE_B_PRODUCT_CODE)
    if product is None:
        product = Product(
            product_code=HOUSE_B_PRODUCT_CODE, product_name="HOUSE_B",
            description="HOUSE_B MVP production master", is_active=True,
        )
        session.add(product)
        session.flush()
    # Product code is the immutable master identity.  Existing display text is
    # preserved because legacy environments use a localized HOUSE_B name.
    return product


def _seed_parts(session: Session) -> None:
    for part_code, part_name, vision_class in HOUSE_B_PARTS:
        part = session.get(Part, part_code)
        if part is None:
            session.add(Part(
                part_code=part_code, part_name=part_name, vision_class=vision_class,
                category=PartCategory.STRUCTURE, unit="EA", is_active=True,
            ))
        elif (part.part_name, part.vision_class, part.category, part.unit) != (
            part_name, vision_class, PartCategory.STRUCTURE, "EA"
        ):
            raise _conflict("Part", part_code, "master fields")
    session.flush()


def _seed_slots(session: Session) -> None:
    for slot_code, display_name in HOUSE_B_SLOTS:
        slot = session.get(InstallationSlot, slot_code)
        if slot is None:
            session.add(InstallationSlot(
                product_code=HOUSE_B_PRODUCT_CODE, slot_code=slot_code,
                display_name=display_name, is_active=True,
            ))
        elif (slot.product_code, slot.display_name) != (HOUSE_B_PRODUCT_CODE, display_name):
            raise _conflict("InstallationSlot", slot_code, "master fields")
    session.flush()


def _expected_stage(stage_data: tuple[object, ...]) -> dict[str, object]:
    order, operation, display, part, slot, zone, option, gate, mode, group, destination, is_terminal = stage_data
    return {
        "stage_order": order, "operation_code": operation, "display_name": display,
        "part_code": part, "quantity": 1, "slot_code": slot, "pick_zone": zone,
        "option_code": option, "execution_gate": gate, "supply_mode": mode,
        "supply_group_code": group, "supply_destination_code": destination,
        "is_terminal": is_terminal,
    }


def _stage_matches(stage: AssemblyRecipeStage, expected: dict[str, object]) -> bool:
    return all(getattr(stage, key) == value for key, value in expected.items())


def _seed_recipe(session: Session) -> AssemblyRecipe:
    recipes = list(session.scalars(
        select(AssemblyRecipe).where(AssemblyRecipe.product_code == HOUSE_B_PRODUCT_CODE)
        .order_by(AssemblyRecipe.version)
    ))
    active = [recipe for recipe in recipes if recipe.is_active]
    if len(active) > 1:
        raise RuntimeError("HOUSE_B has multiple active AssemblyRecipe rows.")
    expected = [_expected_stage(data) for data in HOUSE_B_STAGES]

    def is_expected(recipe: AssemblyRecipe) -> bool:
        stages = list(session.scalars(
            select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)
            .order_by(AssemblyRecipeStage.stage_order)
        ))
        return len(stages) == len(expected) and all(
            _stage_matches(stage, expected_stage)
            for stage, expected_stage in zip(stages, expected, strict=True)
        )

    recipe = next((item for item in recipes if is_expected(item)), None)
    if recipe is None:
        # Recipe rows are immutable.  Preserve a legacy HOUSE_B recipe and add
        # a new version rather than repurposing its stages.
        recipe = AssemblyRecipe(
            product_code=HOUSE_B_PRODUCT_CODE,
            version=(max((item.version for item in recipes), default=0) + 1),
            is_active=False, description=HOUSE_B_RECIPE_DESCRIPTION,
        )
        session.add(recipe)
        session.flush()
        session.add_all(AssemblyRecipeStage(recipe_id=recipe.recipe_id, **_expected_stage(data)) for data in HOUSE_B_STAGES)
        session.flush()

    for existing in active:
        if existing.recipe_id != recipe.recipe_id:
            existing.is_active = False
    recipe.is_active = True
    return recipe


def seed_house_b_mvp_master(session: Session) -> AssemblyRecipe:
    """Add or verify only the approved HOUSE_B master/recipe rows.

    The caller owns commit/rollback, making this usable in guarded test transactions.
    """
    _seed_product(session)
    _seed_parts(session)
    _seed_slots(session)
    return _seed_recipe(session)


def benchmark_session_factory() -> sessionmaker[Session]:
    settings = get_settings()
    production_url = settings.database_url.strip()
    benchmark_url = settings.postgres_test_database_url.strip()
    if not benchmark_url:
        raise RuntimeError("POSTGRES_TEST_DATABASE_URL is required for the HOUSE_B benchmark seed.")
    if not production_url or benchmark_url == production_url:
        raise RuntimeError("POSTGRES_TEST_DATABASE_URL must be non-empty and distinct from DATABASE_URL.")
    parsed = make_url(benchmark_url)
    if not parsed.drivername.startswith("postgresql") or parsed.database != "smart_factory_benchmark":
        raise RuntimeError("HOUSE_B seed CLI only permits PostgreSQL database smart_factory_benchmark.")
    engine = create_engine(benchmark_url, pool_pre_ping=True)
    with engine.connect() as connection:
        actual_database = connection.scalar(text("SELECT current_database()"))
    if actual_database != "smart_factory_benchmark":
        engine.dispose()
        raise RuntimeError("Refusing to seed a database other than smart_factory_benchmark.")
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def main() -> int:
    factory = benchmark_session_factory()
    try:
        with factory() as session:
            recipe = seed_house_b_mvp_master(session)
            session.commit()
            print(f"HOUSE_B MVP master verified in smart_factory_benchmark (recipe_id={recipe.recipe_id}).")
    except Exception as exc:
        print(f"HOUSE_B MVP seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        factory.kw["bind"].dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
