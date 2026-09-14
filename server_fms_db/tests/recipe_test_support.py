from collections import defaultdict

from sqlalchemy import select
from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage, InstallationSlot, Inventory, JobMaterialDelivery, JobMaterialDeliveryItem, MaterialInspection, MaterialInspectionResult, MaterialInspectionStatus, Part, PartCategory, ProductionJob, SupplyMode
from shared.services.inventory_reservation_service import InventoryReservationService

HOUSE_A_STAGES = [
    (1, "INSTALL_BASE", "바닥(Base) 설치"), (2, "INSTALL_TOILET", "변기 설치"), (3, "INSTALL_BASIN", "세면대 설치"), (4, "INSTALL_KITCHEN_SINK", "싱크대 설치"), (5, "INSTALL_COOKTOP", "가스레인지/조리대 설치"), (6, "INSTALL_REFRIGERATOR", "냉장고 설치"), (7, "INSTALL_WASHING_MACHINE", "세탁기 설치"), (8, "INSTALL_COMMON_INNER_WALL", "공용 내벽 설치"), (9, "INSTALL_HOUSE_A_INNER_WALL", "House A 전용 내벽 설치"), (10, "INSTALL_LEFT_OUTER_WALL", "좌측 외벽 설치"), (11, "INSTALL_DOOR_OUTER_WALL", "출입문용 외벽 설치"), (12, "INSTALL_DOOR", "출입문 설치"), (13, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치"), (14, "INSTALL_WINDOW_01", "창문 1 설치"), (15, "INSTALL_REAR_OUTER_WALL", "후면 외벽 설치"), (16, "INSTALL_WINDOW_02", "창문 2 설치"),
]
HOUSE_B_STAGES = [
    (1, "INSTALL_BASE", "바닥(Base) 설치"), (2, "INSTALL_TOILET", "변기 설치"), (3, "INSTALL_BASIN", "세면대 설치"), (4, "INSTALL_BATHTUB", "욕조 설치"), (5, "INSTALL_WASHING_MACHINE", "세탁기 설치"), (6, "INSTALL_COOKTOP", "가스레인지/조리대 설치"), (7, "INSTALL_KITCHEN_SINK", "싱크대 설치"), (8, "INSTALL_REFRIGERATOR", "냉장고 설치"), (9, "INSTALL_COMMON_INNER_WALL", "공용 내벽 설치"), (10, "INSTALL_RIGHT_OUTER_WALL", "우측 외벽 설치"), (11, "INSTALL_REAR_OUTER_WALL", "후면 외벽 설치"), (12, "INSTALL_WINDOW_01", "창문 1 설치"), (13, "INSTALL_LEFT_OUTER_WALL", "좌측 외벽 설치"), (14, "INSTALL_WINDOW_02", "창문 2 설치"), (15, "INSTALL_DOOR_OUTER_WALL", "출입문용 외벽 설치"), (16, "INSTALL_DOOR", "출입문 설치"),
]

def add_material_requirement(session, recipe, *, part_code=None, inventory_quantity=None, stage_order=None):
    part_code = part_code or f"TEST_MATERIAL_{recipe.product_code}_{recipe.recipe_id}"
    part = session.get(Part, part_code)
    if part is None:
        part = Part(
            part_code=part_code,
            part_name="Test material",
            vision_class="wall_ext_back",
            category=PartCategory.STRUCTURE,
            unit="EA",
        )
        session.add(part)
        session.flush()
    stage = (
        next(item for item in recipe.stages if item.stage_order == stage_order)
        if stage_order is not None
        else max(
            (item for item in recipe.stages if item.execution_gate is None),
            key=lambda item: item.stage_order,
        )
    )
    stage.part_code = part.part_code
    stage.quantity = 1
    if inventory_quantity is not None:
        inventory = session.get(Inventory, part.part_code)
        if inventory is None:
            session.add(Inventory(part_code=part.part_code, quantity=inventory_quantity))
        else:
            inventory.quantity = inventory_quantity
    session.flush()
    return part


def add_gated_roof_stages(session, recipe):
    """Add synthetic gated Roof master rows only to tests exercising PRE_ROOF."""
    # Gated stages become the linear recipe tail; the former tail cannot remain terminal.
    for stage in recipe.stages:
        stage.is_terminal = False
    next_order = max(stage.stage_order for stage in recipe.stages) + 1
    for offset, (roof_code, vision_class) in enumerate((("ROOF_01", "roof_01"), ("ROOF_02", "roof_02"))):
        if session.get(Part, roof_code) is None:
            session.add(Part(
                part_code=roof_code, part_name=f"Synthetic {roof_code}",
                category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA",
            ))
            session.flush()
        if session.get(Inventory, roof_code) is None:
            # Generic test fixture stock for the selected full recipe,
            # including the delayed PRE_ROOF-gated tail.
            session.add(Inventory(part_code=roof_code, quantity=100))
        slot_code = f"TEST_{recipe.product_code}_{roof_code}_SLOT"
        if session.get(InstallationSlot, slot_code) is None:
            session.add(InstallationSlot(
                product_code=recipe.product_code, slot_code=slot_code,
                display_name=f"Synthetic {roof_code} slot",
            ))
        session.flush()
        session.add(AssemblyRecipeStage(
            recipe_id=recipe.recipe_id, stage_order=next_order + offset,
            operation_code="INSTALL_ROOF", display_name=f"Synthetic {roof_code} roof",
            part_code=roof_code, quantity=1, slot_code=slot_code, pick_zone="TEST_ROOF_ZONE",
            option_code=roof_code, execution_gate="PRE_ROOF_PASS",
            supply_mode=SupplyMode.MANUAL, supply_group_code=f"TEST_ROOF_GROUP_{roof_code}",
            is_terminal=True,
        ))
    session.flush()

def add_active_recipe(session, product_code, *, version=1, stages=None, materialized=True):
    if stages is None:
        stages = HOUSE_A_STAGES if product_code == 'HOUSE_A' else HOUSE_B_STAGES if product_code == 'HOUSE_B' else [(i, f'TEST_OPERATION_{i:02d}', f'Test stage {i}') for i in range(1, 17)]
    recipe = AssemblyRecipe(product_code=product_code, version=version, is_active=True, description='test recipe')
    session.add(recipe); session.flush()
    session.add_all(AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=order, operation_code=operation, display_name=name, is_terminal=order == max(item[0] for item in stages)) for order, operation, name in stages)
    session.flush()
    if materialized:
        add_material_requirement(session, recipe, inventory_quantity=100)
    return recipe


def seed_inventory_for_recipe(session, recipe: AssemblyRecipe, *, units_per_requirement: int = 10) -> None:
    """Seed finite test-only physical stock for every materialized recipe requirement.

    This fixture helper derives codes and quantities from the recipe, including
    deferred/gated stages, and is intentionally never used by production code.
    """
    if units_per_requirement < 1:
        raise ValueError("units_per_requirement must be positive")
    requirements: dict[str, int] = defaultdict(int)
    for stage in recipe.stages:
        if stage.part_code and isinstance(stage.quantity, int) and stage.quantity > 0:
            requirements[stage.part_code] += stage.quantity
    for part_code, quantity in requirements.items():
        inventory = session.get(Inventory, part_code)
        required_stock = quantity * units_per_requirement
        if inventory is None:
            session.add(Inventory(part_code=part_code, quantity=required_stock))
        else:
            inventory.quantity = max(inventory.quantity, inventory.reserved_quantity + required_stock)
    session.flush()


def seed_and_reserve_inventory_for_job(session, *, job: ProductionJob, units_per_requirement: int = 10) -> None:
    """Prepare a manually constructed test Job for the real issue path.

    Production Jobs normally reserve in ``create_job``. Tests that intentionally
    construct legacy JobStep/Delivery snapshots directly use this explicit
    fixture seam so accepted Cell execution still observes the production
    reservation invariant.
    """
    if units_per_requirement < 1:
        raise ValueError("units_per_requirement must be positive")
    items = list(session.scalars(
        select(JobMaterialDeliveryItem)
        .join(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job.job_id)
    ))
    requirements: dict[str, int] = defaultdict(int)
    for item in items:
        if item.part_code and isinstance(item.quantity, int) and item.quantity > 0:
            requirements[item.part_code] += item.quantity
    for part_code, quantity in requirements.items():
        inventory = session.get(Inventory, part_code)
        required_stock = quantity * units_per_requirement
        if inventory is None:
            session.add(Inventory(part_code=part_code, quantity=required_stock))
        else:
            inventory.quantity = max(inventory.quantity, inventory.reserved_quantity + required_stock)
    session.flush()
    # Direct legacy fixtures can call this once per QA item. They are isolated
    # single-Job fixtures, so a reservation already covering their complete
    # current snapshot is treated as setup-idempotent rather than reserved
    # again.
    if all(
        (inventory := session.get(Inventory, part_code)) is not None
        and inventory.reserved_quantity >= quantity
        for part_code, quantity in requirements.items()
    ):
        return
    InventoryReservationService(session).reserve_job_requirements(job=job)


def seed_complete_incoming_qa(session, *, job_id: int) -> int:
    """Create explicit latest PASS evidence for every persisted expected item."""
    items = list(session.scalars(
        select(JobMaterialDeliveryItem)
        .join(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job_id)
        .order_by(JobMaterialDeliveryItem.delivery_item_id)
    ))
    for item in items:
        session.add(MaterialInspection(
            inspection_request_id=f"test-pass-{job_id}-{item.delivery_item_id}",
            delivery_item_id=item.delivery_item_id, inspection_cycle=1,
            status=MaterialInspectionStatus.COMPLETED, result=MaterialInspectionResult.PASS,
            expected_part_code=item.part_code, expected_class_name=item.part.vision_class,
            expected_quantity=item.quantity, detected_quantity=item.quantity, production_valid=True,
        ))
    session.commit()
    return len(items)
