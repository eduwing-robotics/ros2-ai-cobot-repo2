from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.replenish_house_a_current_benchmark_inventory import (
    HOUSE_A_CURRENT_INVENTORY_PARTS,
    TARGET_AVAILABLE_QUANTITY,
    replenish_house_a_current_benchmark_inventory,
)
from scripts.seed_house_a_mvp_master import seed_house_a_current_master
from shared.models import Base
from shared.models.factory import Inventory, InventoryMovement, MovementType


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        yield db
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_house_a_replenishment_uses_stock_in_and_is_idempotent(session: Session) -> None:
    seed_house_a_current_master(session)
    session.commit()

    first = replenish_house_a_current_benchmark_inventory(session)
    assert [(row.part_code, row.before_available_quantity, row.after_available_quantity) for row in first] == [
        (part_code, 0, TARGET_AVAILABLE_QUANTITY)
        for part_code in HOUSE_A_CURRENT_INVENTORY_PARTS
    ]
    rows = list(session.scalars(select(Inventory).where(
        Inventory.part_code.in_(HOUSE_A_CURRENT_INVENTORY_PARTS)
    ).order_by(Inventory.part_code)))
    assert {row.part_code: (row.quantity, row.reserved_quantity, row.available_quantity) for row in rows} == {
        part_code: (TARGET_AVAILABLE_QUANTITY, 0, TARGET_AVAILABLE_QUANTITY)
        for part_code in HOUSE_A_CURRENT_INVENTORY_PARTS
    }
    assert session.scalar(select(func.count()).select_from(InventoryMovement).where(
        InventoryMovement.part_code.in_(HOUSE_A_CURRENT_INVENTORY_PARTS),
        InventoryMovement.movement_type == MovementType.IN,
    )) == len(HOUSE_A_CURRENT_INVENTORY_PARTS)

    second = replenish_house_a_current_benchmark_inventory(session)
    assert all(row.before_available_quantity == TARGET_AVAILABLE_QUANTITY for row in second)
    assert all(row.after_available_quantity == TARGET_AVAILABLE_QUANTITY for row in second)
    assert session.scalar(select(func.count()).select_from(InventoryMovement).where(
        InventoryMovement.part_code.in_(HOUSE_A_CURRENT_INVENTORY_PARTS),
        InventoryMovement.movement_type == MovementType.IN,
    )) == len(HOUSE_A_CURRENT_INVENTORY_PARTS)
