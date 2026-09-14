"""Fail-closed validation for materialized production recipe configuration.

This module validates configuration, not stock. It is shared by inventory
preflight and job materialization so an empty or malformed material recipe can
never be mistaken for sufficient inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage


@dataclass(frozen=True)
class ProductionConfigurationIssue:
    code: str
    recipe_stage_id: int | None
    stage_order: int
    detail: str


class ProductionConfigurationInvalidError(RuntimeError):
    """Raised when a recipe cannot safely materialize a production job."""

    def __init__(
        self,
        *,
        product_code: str,
        recipe_id: int | None,
        issues: tuple[ProductionConfigurationIssue, ...],
    ) -> None:
        self.product_code = product_code
        self.recipe_id = recipe_id
        self.issues = issues
        super().__init__(
            "Production configuration is invalid: "
            + "; ".join(f"{issue.code} at stage {issue.stage_order}" for issue in issues)
        )


class ProductionConfigurationValidator:
    """Validate only the current material-binding invariants.

    It deliberately does not require logistics fields, slots, zones, or a
    complete future BOM. It prevents the confirmed fail-open only: no usable
    material requirements, or a stage that partially declares one.
    """

    @staticmethod
    def validate(
        *, recipe: AssemblyRecipe, stages: Iterable[AssemblyRecipeStage]
    ) -> None:
        issues: list[ProductionConfigurationIssue] = []
        usable_material_count = 0

        for stage in stages:
            part_code = stage.part_code.strip() if isinstance(stage.part_code, str) else ""
            quantity = stage.quantity
            has_part = bool(part_code)
            has_quantity = quantity is not None

            if has_part and quantity is None:
                issues.append(
                    ProductionConfigurationIssue(
                        code="MISSING_MATERIAL_QUANTITY",
                        recipe_stage_id=stage.recipe_stage_id,
                        stage_order=stage.stage_order,
                        detail="part_code is configured but quantity is missing.",
                    )
                )
                continue
            if has_quantity and not has_part:
                issues.append(
                    ProductionConfigurationIssue(
                        code="MISSING_MATERIAL_PART_CODE",
                        recipe_stage_id=stage.recipe_stage_id,
                        stage_order=stage.stage_order,
                        detail="quantity is configured but part_code is missing.",
                    )
                )
                continue
            if has_part and quantity is not None and quantity <= 0:
                issues.append(
                    ProductionConfigurationIssue(
                        code="NONPOSITIVE_MATERIAL_QUANTITY",
                        recipe_stage_id=stage.recipe_stage_id,
                        stage_order=stage.stage_order,
                        detail="material quantity must be positive.",
                    )
                )
                continue
            if has_part and quantity is not None:
                usable_material_count += 1

        if usable_material_count == 0:
            issues.append(
                ProductionConfigurationIssue(
                    code="ZERO_MATERIAL_REQUIREMENTS",
                    recipe_stage_id=None,
                    stage_order=0,
                    detail="recipe has no usable part_code and positive quantity material requirement.",
                )
            )

        if issues:
            raise ProductionConfigurationInvalidError(
                product_code=recipe.product_code,
                recipe_id=recipe.recipe_id,
                issues=tuple(issues),
            )

    @staticmethod
    def validate_terminal_lifecycle(
        *, recipe: AssemblyRecipe, stages: Iterable[AssemblyRecipeStage]
    ) -> None:
        ordered = sorted(stages, key=lambda stage: stage.stage_order)
        terminals = [stage for stage in ordered if stage.is_terminal is True]
        issues: list[ProductionConfigurationIssue] = []
        if len(terminals) == 0:
            issues.append(ProductionConfigurationIssue(code="MISSING_TERMINAL_STAGE", recipe_stage_id=None, stage_order=0, detail="linear recipe requires exactly one terminal stage."))
        elif len(terminals) > 1:
            issues.append(ProductionConfigurationIssue(code="MULTIPLE_TERMINAL_STAGES", recipe_stage_id=None, stage_order=0, detail="linear recipe permits exactly one terminal stage."))
        elif terminals[0].stage_order != ordered[-1].stage_order:
            issues.append(ProductionConfigurationIssue(code="TERMINAL_STAGE_NOT_FINAL", recipe_stage_id=terminals[0].recipe_stage_id, stage_order=terminals[0].stage_order, detail="terminal stage must have the greatest stage_order."))
        if issues:
            raise ProductionConfigurationInvalidError(product_code=recipe.product_code, recipe_id=recipe.recipe_id, issues=tuple(issues))
