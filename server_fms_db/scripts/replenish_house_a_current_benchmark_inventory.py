"""Guarded benchmark replenishment for the current HOUSE_A production Recipe.

Only existing HOUSE_A v2 Parts are stocked.  The script uses the normal
InventoryService stock-in path, which writes both inventory projection and IN
movement history.  It refuses every database other than smart_factory_benchmark.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session

from scripts.seed_house_a_mvp_master import benchmark_session_factory
from shared.models.factory import Inventory, Part
from shared.services.inventory_service import InventoryService, PartNotFoundError

TARGET_AVAILABLE_QUANTITY = 10
HOUSE_A_CURRENT_INVENTORY_PARTS = (
    "BASE-HOUSE-A-01",
    "ROOF-NOZIP-01",
    "WALL-INT-HOUSE-A-01",
    "WALL-INT-HOUSE-A-DOOR-01",
)


@dataclass(frozen=True)
class ReplenishmentResult:
    part_code: str
    before_quantity: int
    before_reserved_quantity: int
    after_quantity: int
    after_reserved_quantity: int

    @property
    def before_available_quantity(self) -> int:
        return self.before_quantity - self.before_reserved_quantity

    @property
    def after_available_quantity(self) -> int:
        return self.after_quantity - self.after_reserved_quantity


def replenish_house_a_current_benchmark_inventory(session: Session) -> tuple[ReplenishmentResult, ...]:
    """Raise only current HOUSE_A v2 Part availability to the configured target.

    Existing reservations are never changed.  Missing Part masters are a
    configuration error; this replenishment intentionally never creates them.
    """
    service = InventoryService(session)
    results: list[ReplenishmentResult] = []
    for part_code in HOUSE_A_CURRENT_INVENTORY_PARTS:
        part = session.get(Part, part_code)
        if part is None:
            raise PartNotFoundError(f"Current HOUSE_A Part master is missing: {part_code!r}.")
        inventory = session.get(Inventory, part_code)
        before_quantity = inventory.quantity if inventory is not None else 0
        before_reserved = inventory.reserved_quantity if inventory is not None else 0
        shortfall = TARGET_AVAILABLE_QUANTITY - (before_quantity - before_reserved)
        if shortfall > 0:
            inventory = service.stock_in(
                part_code=part_code,
                quantity=shortfall,
                reason="Benchmark HOUSE_A current recipe test replenishment.",
            )
        else:
            inventory = session.get(Inventory, part_code)
            assert inventory is not None
        results.append(ReplenishmentResult(
            part_code=part_code,
            before_quantity=before_quantity,
            before_reserved_quantity=before_reserved,
            after_quantity=inventory.quantity,
            after_reserved_quantity=inventory.reserved_quantity,
        ))
    return tuple(results)


def main() -> int:
    factory = benchmark_session_factory()
    try:
        with factory() as session:
            results = replenish_house_a_current_benchmark_inventory(session)
        for result in results:
            print(
                f"{result.part_code}: quantity {result.before_quantity} -> {result.after_quantity}; "
                f"reserved {result.before_reserved_quantity} -> {result.after_reserved_quantity}; "
                f"available {result.before_available_quantity} -> {result.after_available_quantity}"
            )
    except Exception as exc:
        print(f"HOUSE_A benchmark inventory replenishment failed: {exc}", file=sys.stderr)
        return 1
    finally:
        factory.kw["bind"].dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
