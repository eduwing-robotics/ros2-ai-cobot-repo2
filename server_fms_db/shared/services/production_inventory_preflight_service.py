from __future__ import annotations

import dataclasses
from sqlalchemy.orm import Session
from sqlalchemy import select

from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage, Part, Inventory, RoofOptionCode
from shared.services.assembly_recipe_service import AssemblyRecipeService
from shared.services.production_configuration_validator import ProductionConfigurationValidator


@dataclasses.dataclass(frozen=True)
class PreflightShortage:
    part_code: str
    part_name: str
    required_quantity: int
    available_quantity: int
    shortage_quantity: int


@dataclasses.dataclass(frozen=True)
class ProductionInventoryPreflightResult:
    can_produce: bool
    product_code: str
    requested_quantity: int
    shortages: list[PreflightShortage]


class ProductionInventoryPreflightService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._recipes = AssemblyRecipeService(session)

    def validate(
        self,
        *,
        product_code: str,
        quantity: int,
        roof_option_code: RoofOptionCode | None = None,
    ) -> ProductionInventoryPreflightResult:
        recipe = self._recipes.get_active_recipe_for_product(product_code)
        # Advisory preflight includes the complete selected Recipe, including
        # PRE_ROOF-gated stages. The create transaction performs the authoritative
        # row-locked reservation and is the TOCTOU-safe decision.
        stages = AssemblyRecipeService.get_ordered_stages(recipe)
        ProductionConfigurationValidator.validate(recipe=recipe, stages=stages)

        part_requirements: dict[str, int] = {}

        for stage in stages:
            if stage.part_code is None or stage.quantity is None or stage.quantity <= 0:
                continue

            if stage.option_code is not None:
                if roof_option_code is None or stage.option_code != roof_option_code.value:
                    continue

            part_requirements[stage.part_code] = part_requirements.get(stage.part_code, 0) + stage.quantity

        shortages: list[PreflightShortage] = []
        for part_code, req_qty_per_item in part_requirements.items():
            total_req_qty = req_qty_per_item * quantity

            part = self._session.scalar(select(Part).where(Part.part_code == part_code))
            if part is None or not part.is_active:
                shortages.append(PreflightShortage(
                    part_code=part_code,
                    part_name=part.part_name if part else part_code,
                    required_quantity=total_req_qty,
                    available_quantity=0,
                    shortage_quantity=total_req_qty
                ))
                continue

            inv = self._session.get(Inventory, part_code)
            available = inv.available_quantity if inv else 0

            if available < total_req_qty:
                shortages.append(PreflightShortage(
                    part_code=part_code,
                    part_name=part.part_name,
                    required_quantity=total_req_qty,
                    available_quantity=available,
                    shortage_quantity=total_req_qty - available
                ))

        # Ensure consistent order for deterministic testing and messaging
        shortages.sort(key=lambda x: x.part_code)

        return ProductionInventoryPreflightResult(
            can_produce=len(shortages) == 0,
            product_code=product_code,
            requested_quantity=quantity,
            shortages=shortages
        )
