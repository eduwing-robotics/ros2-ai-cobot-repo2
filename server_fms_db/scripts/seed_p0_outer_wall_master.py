import sys
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.models.factory import (
    Product, Part, InstallationSlot, AssemblyRecipe, AssemblyRecipeStage
)
from shared.database import get_session_factory

PARTS_DATA = [
    {
        "part_code": "WALL-EXT-BACK-BLK01",
        "vision_class": "wall_ext_back",
        "part_name": "후면 외벽 (블랙)",
        "category": "STRUCTURE",
        "unit": "EA",
        "is_active": True,
    },
    {
        "part_code": "WALL-EXT-DOOR-BLK01",
        "vision_class": "wall_ext_door",
        "part_name": "출입문용 외벽 (블랙)",
        "category": "STRUCTURE",
        "unit": "EA",
        "is_active": True,
    },
    {
        "part_code": "WALL-EXT-LEFT-BLK01",
        "vision_class": "wall_ext_left",
        "part_name": "좌측 외벽 (블랙)",
        "category": "STRUCTURE",
        "unit": "EA",
        "is_active": True,
    },
    {
        "part_code": "WALL-EXT-RIGHT-BLK01",
        "vision_class": "wall_ext_right",
        "part_name": "우측 외벽 (블랙)",
        "category": "STRUCTURE",
        "unit": "EA",
        "is_active": True,
    },
]

SLOTS_DATA = [
    {
        "slot_code": "HOUSE_A_OUTER_WALL_REAR_01",
        "display_name": "후면 외벽 설치 슬롯",
        "is_active": True,
    },
    {
        "slot_code": "HOUSE_A_OUTER_WALL_DOOR_01",
        "display_name": "출입문용 외벽 설치 슬롯",
        "is_active": True,
    },
    {
        "slot_code": "HOUSE_A_OUTER_WALL_LEFT_01",
        "display_name": "좌측 외벽 설치 슬롯",
        "is_active": True,
    },
    {
        "slot_code": "HOUSE_A_OUTER_WALL_RIGHT_01",
        "display_name": "우측 외벽 설치 슬롯",
        "is_active": True,
    },
]

BINDINGS = {
    "INSTALL_REAR_OUTER_WALL": {
        "part_code": "WALL-EXT-BACK-BLK01",
        "slot_code": "HOUSE_A_OUTER_WALL_REAR_01",
        "pick_zone": "CONVEYOR_PICK",
        "quantity": 1,
    },
    "INSTALL_DOOR_OUTER_WALL": {
        "part_code": "WALL-EXT-DOOR-BLK01",
        "slot_code": "HOUSE_A_OUTER_WALL_DOOR_01",
        "pick_zone": "CONVEYOR_PICK",
        "quantity": 1,
    },
    "INSTALL_LEFT_OUTER_WALL": {
        "part_code": "WALL-EXT-LEFT-BLK01",
        "slot_code": "HOUSE_A_OUTER_WALL_LEFT_01",
        "pick_zone": "CONVEYOR_PICK",
        "quantity": 1,
    },
    "INSTALL_RIGHT_OUTER_WALL": {
        "part_code": "WALL-EXT-RIGHT-BLK01",
        "slot_code": "HOUSE_A_OUTER_WALL_RIGHT_01",
        "pick_zone": "CONVEYOR_PICK",
        "quantity": 1,
    },
}

def seed_p0_outer_walls(session: Session) -> None:
    # 1. Check Product exists
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    if not product:
        raise RuntimeError("Product 'HOUSE_A' not found. Cannot proceed with seed.")

    # 2. Seed Parts
    for data in PARTS_DATA:
        part = session.get(Part, data["part_code"])
        if part is None:
            part = Part(**data)
            session.add(part)
            print(f"Created Part: {part.part_code}")
        else:
            if part.vision_class != data["vision_class"]:
                raise RuntimeError(f"Part conflict for {part.part_code}: vision_class mismatch")
            print(f"Part exists: {part.part_code}")

    # 3. Seed Slots
    for data in SLOTS_DATA:
        slot = session.get(InstallationSlot, data["slot_code"])
        if slot is None:
            slot = InstallationSlot(product_code="HOUSE_A", **data)
            session.add(slot)
            print(f"Created Slot: {slot.slot_code}")
        else:
            if slot.product_code != "HOUSE_A":
                raise RuntimeError(f"Slot conflict for {slot.slot_code}: product_code mismatch")
            print(f"Slot exists: {slot.slot_code}")

    # 4. Bind to Active Recipe
    recipe = session.scalar(
        select(AssemblyRecipe).where(
            AssemblyRecipe.product_code == "HOUSE_A",
            AssemblyRecipe.is_active.is_(True)
        )
    )
    if not recipe:
        raise RuntimeError("Active AssemblyRecipe for HOUSE_A not found.")

    stages = session.scalars(
        select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)
    ).all()

    for stage in stages:
        if stage.operation_code in BINDINGS:
            binding = BINDINGS[stage.operation_code]
            stage.part_code = binding["part_code"]
            stage.slot_code = binding["slot_code"]
            stage.pick_zone = binding["pick_zone"]
            stage.quantity = binding["quantity"]
            print(f"Bound Stage: {stage.operation_code} -> {stage.part_code} @ {stage.slot_code}")

    session.commit()
    print("Seeding successful.")

def main() -> int:
    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            seed_p0_outer_walls(session)
    except Exception as e:
        print(f"Failed to seed: {e}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
