"""Read-only master-scoped inventory projection for Voice and text AI routes."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from api_server.services.temporary_product_catalog import find_product
from sqlalchemy.orm import Session

from shared.enums.ai import Intent
from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage, Inventory, Part, Product
from shared.schemas.ai import StructuredCommand
from shared.schemas.inventory import VoiceInventoryItem, VoiceInventoryProductContext
from shared.services.inventory_service import InventoryService


class InventoryVoiceQueryService:
    """Resolve Voice inventory terms against active Product/Recipe master data.

    This intentionally leaves ``InventoryService.get_inventory_by_identifier``
    unchanged for its existing single-part callers. Voice is read-only and may
    return every matching active-recipe material without clarification.
    """

    def __init__(self, inventory_service: InventoryService) -> None:
        self._inventory_service = inventory_service

    def fetch(self, command: StructuredCommand) -> list[VoiceInventoryItem] | None:
        if command.intent is not Intent.QUERY_INVENTORY:
            return None
        if command.inventory_scope == "CATEGORY":
            return None

        rows = self._active_master_rows()
        if not rows:
            return self._legacy_projection(command)
        product_scope, material_term = self._resolve_product_scope(command, rows)
        if product_scope is not None:
            rows = [row for row in rows if row[0].product_code == product_scope]
        if material_term:
            rows = self._filter_material_term(rows, material_term)
        projected = self._project(rows)
        # Exact-code compatibility is intentionally broader than active recipe
        # browsing: a known physical Part remains queryable even when master
        # recipe configuration is incomplete.
        if command.item_name and not projected:
            return self._legacy_projection(command)
        return projected

    @property
    def _session(self) -> Session:
        return self._inventory_service.session

    def _active_master_rows(self):
        return list(self._session.execute(
            select(Product, AssemblyRecipe, AssemblyRecipeStage, Part, Inventory)
            .join(AssemblyRecipe, AssemblyRecipe.product_code == Product.product_code)
            .join(AssemblyRecipeStage, AssemblyRecipeStage.recipe_id == AssemblyRecipe.recipe_id)
            .join(Part, Part.part_code == AssemblyRecipeStage.part_code)
            .outerjoin(Inventory, Inventory.part_code == Part.part_code)
            .where(
                AssemblyRecipe.is_active.is_(True),
                AssemblyRecipeStage.part_code.is_not(None),
            )
            .order_by(Product.product_code, Part.part_code, AssemblyRecipeStage.stage_order)
        ))

    def _resolve_product_scope(self, command: StructuredCommand, rows) -> tuple[str | None, str | None]:
        active_products = {
            product.product_code: product
            for product, _recipe, _stage, _part, _inventory in rows
        }
        if command.product_code and command.product_code.strip() in active_products:
            return command.product_code.strip(), self._normalized_term(command.item_name)

        if command.product_name:
            scope = self._find_product_name_scope(command.product_name, active_products)
            if scope is not None:
                return scope, self._normalized_term(command.item_name)

        # Deterministic parser intentionally remains DB-independent. Resolve a
        # leading product alias here from Product master rows, then keep the
        # remainder as the material term. Product name/code is the authority;
        # no part-code parsing or HOUSE-specific alias table is used.
        term = self._normalized_term(command.item_name)
        if not term:
            return None, None
        # Reuse the established legacy product alias resolver where it knows
        # an alias such as B형, but validate its canonical code against the
        # active DB master before applying it. New products continue to work
        # through the master-derived aliases below.
        catalog_product = find_product(term)
        if catalog_product is not None and catalog_product.code in active_products:
            aliases = sorted(
                (alias.casefold() for alias in catalog_product.aliases),
                key=len,
                reverse=True,
            )
            for alias in aliases:
                if term == alias:
                    return catalog_product.code, None
                if term.startswith(f"{alias} "):
                    return catalog_product.code, term[len(alias):].strip() or None
        for alias, code in self._master_product_aliases(active_products):
            if term == alias:
                return code, None
            if term.startswith(f"{alias} "):
                return code, term[len(alias):].strip() or None
        return None, term

    @staticmethod
    def _normalized_term(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = " ".join(value.split()).strip()
        return normalized.casefold() if normalized else None

    def _find_product_name_scope(self, value: str, active_products: dict[str, Product]) -> str | None:
        normalized = self._normalized_term(value)
        if normalized is None:
            return None
        for alias, code in self._master_product_aliases(active_products):
            if normalized == alias:
                return code
        return None

    @staticmethod
    def _master_product_aliases(active_products: dict[str, Product]) -> list[tuple[str, str]]:
        aliases: set[tuple[str, str]] = set()
        for code, product in active_products.items():
            aliases.add((code.casefold(), code))
            name = " ".join(product.product_name.split()).casefold()
            if name:
                aliases.add((name, code))
                # Product names commonly contain a descriptive second word
                # (for example a model name plus product class). The first
                # master-derived token remains a deterministic short alias.
                first_token = name.split(" ", 1)[0]
                if first_token:
                    aliases.add((first_token, code))
        return sorted(aliases, key=lambda item: (-len(item[0]), item[0], item[1]))

    @staticmethod
    def _filter_material_term(rows, term: str):
        # Exact canonical part_code wins. Exact human name then generic
        # master-text family matching (Part name or recipe stage display name)
        # permits terms such as 외벽 and 지붕 without product-specific lists.
        exact_code = [row for row in rows if row[3].part_code.casefold() == term]
        if exact_code:
            return exact_code
        exact_name = [row for row in rows if row[3].part_name.casefold() == term]
        if exact_name:
            return exact_name
        return [
            row for row in rows
            if term in row[3].part_name.casefold()
            or term in row[2].display_name.casefold()
        ]

    def _legacy_projection(self, command: StructuredCommand) -> list[VoiceInventoryItem]:
        """Preserve legacy exact-Part behavior where no active master exists."""
        if command.item_name:
            found = self._inventory_service.get_inventory_by_identifier(command.item_name)
            if found is None:
                return []
            part, inventory = found
            return [VoiceInventoryItem(
                part_code=part.part_code,
                part_name=part.part_name or part.part_code,
                quantity=inventory.quantity if inventory is not None else 0,
                reserved_quantity=inventory.reserved_quantity if inventory is not None else 0,
                available_quantity=inventory.available_quantity if inventory is not None else 0,
                updated_at=inventory.updated_at if inventory is not None else None,
                products=[],
            )]
        return [
            VoiceInventoryItem(
                part_code=inventory.part.part_code,
                part_name=inventory.part.part_name or inventory.part.part_code,
                quantity=inventory.quantity,
                reserved_quantity=inventory.reserved_quantity,
                available_quantity=inventory.available_quantity,
                updated_at=inventory.updated_at,
                products=[],
            )
            for inventory in self._inventory_service.get_all_inventory()
        ]

    @staticmethod
    def _project(rows) -> list[VoiceInventoryItem]:
        grouped: dict[str, dict] = {}
        for product, _recipe, stage, part, inventory in rows:
            entry = grouped.setdefault(
                part.part_code,
                {
                    "part_code": part.part_code,
                    "part_name": part.part_name,
                    "quantity": inventory.quantity if inventory is not None else 0,
                    "reserved_quantity": inventory.reserved_quantity if inventory is not None else 0,
                    "available_quantity": inventory.available_quantity if inventory is not None else 0,
                    "updated_at": inventory.updated_at if inventory is not None else None,
                    "products": {},
                    # A physical Part is narrated as an option only when every
                    # active recipe association declares an option_code. A Part
                    # also used structurally remains product-contextual.
                    "is_option_material": stage.option_code is not None,
                },
            )
            entry["products"][product.product_code] = product.product_name
            entry["is_option_material"] = entry["is_option_material"] and stage.option_code is not None

        result: list[VoiceInventoryItem] = []
        for entry in grouped.values():
            products = [
                VoiceInventoryProductContext(product_code=code, product_name=name)
                for code, name in sorted(entry.pop("products").items())
            ]
            is_option_material = entry.pop("is_option_material")
            result.append(VoiceInventoryItem(**entry, products=products).mark_option_material(is_option_material))
        return sorted(result, key=lambda item: item.part_code)
