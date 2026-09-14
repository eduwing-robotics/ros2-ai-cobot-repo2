"""Read-only lookup for the versioned assembly recipe selected for new jobs."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage


class AssemblyRecipeError(RuntimeError):
    """Base error for recipe lookup failures."""


class ActiveAssemblyRecipeNotFoundError(AssemblyRecipeError):
    """Raised when a product has no current recipe for new production jobs."""


class ActiveAssemblyRecipeIntegrityError(AssemblyRecipeError):
    """Raised when data unexpectedly resolves to multiple active recipes."""


class AssemblyRecipeService:
    """Resolve the one active, versioned assembly recipe for a product."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active_recipe_for_product(
        self, product_code: str, *, for_update: bool = False
    ) -> AssemblyRecipe:
        statement = (
            select(AssemblyRecipe)
            .options(selectinload(AssemblyRecipe.stages))
            .where(
                AssemblyRecipe.product_code == product_code,
                AssemblyRecipe.is_active.is_(True),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        recipes = list(self._session.scalars(statement))
        if not recipes:
            raise ActiveAssemblyRecipeNotFoundError(
                f"No active assembly recipe configured for product_code={product_code!r}."
            )
        if len(recipes) != 1:
            raise ActiveAssemblyRecipeIntegrityError(
                f"Expected exactly one active assembly recipe for product_code={product_code!r}; found {len(recipes)}."
            )
        recipe = recipes[0]
        if not recipe.stages:
            raise ActiveAssemblyRecipeIntegrityError(
                f"Active assembly recipe recipe_id={recipe.recipe_id} has no stages."
            )
        return recipe


    def get_recipe_for_product(
        self, *, recipe_id: int, product_code: str
    ) -> AssemblyRecipe:
        recipe = self._session.scalar(
            select(AssemblyRecipe)
            .options(selectinload(AssemblyRecipe.stages))
            .where(
                AssemblyRecipe.recipe_id == recipe_id,
                AssemblyRecipe.product_code == product_code,
            )
        )
        if recipe is None:
            raise ActiveAssemblyRecipeIntegrityError(
                f"Assembly recipe recipe_id={recipe_id} does not belong to product_code={product_code!r}."
            )
        if not recipe.stages:
            raise ActiveAssemblyRecipeIntegrityError(
                f"Assembly recipe recipe_id={recipe.recipe_id} has no stages."
            )
        return recipe

    @staticmethod
    def get_ordered_stages(recipe: AssemblyRecipe) -> list[AssemblyRecipeStage]:
        return sorted(recipe.stages, key=lambda stage: stage.stage_order)
