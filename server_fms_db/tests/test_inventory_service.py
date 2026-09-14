from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from shared.models.factory import Inventory, InventoryMovement, MovementType, Part, PartCategory
from shared.services.inventory_service import (
    InsufficientInventoryError,
    InvalidInventoryInputError,
    InventoryService,
    JobNotFoundError,
    PartNotFoundError,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def service(session: Session) -> InventoryService:
    return InventoryService(session)


def create_part(session: Session, *, code: str = "TEST_PART") -> Part:
    part = Part(
        vision_class="wall_ext_back",
        part_code=code,
        part_name=f"{code} name",
        category=PartCategory.STRUCTURE,
        unit="EA",
    )
    session.add(part)
    session.commit()
    return part


def movements_for(session: Session, part_code: str) -> list[InventoryMovement]:
    return list(
        session.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.part_code == part_code)
            .order_by(InventoryMovement.movement_id)
        )
    )


def test_stock_in_creates_inventory_and_in_movement(session: Session, service: InventoryService) -> None:
    part = create_part(session)

    inventory = service.stock_in(part_code=part.part_code, quantity=10, reason="initial receiving")

    assert inventory.quantity == 10
    stored = session.get(Inventory, part.part_code)
    assert stored is not None
    assert stored.quantity == 10
    movements = movements_for(session, part.part_code)
    assert [(movement.movement_type, movement.quantity, movement.reason) for movement in movements] == [
        (MovementType.IN, 10, "initial receiving")
    ]


def test_additional_stock_in_accumulates_and_keeps_history(session: Session, service: InventoryService) -> None:
    part = create_part(session)
    service.stock_in(part_code=part.part_code, quantity=10, reason="first receiving")
    service.stock_in(part_code=part.part_code, quantity=5, reason="second receiving")

    assert session.get(Inventory, part.part_code).quantity == 15
    assert [(movement.movement_type, movement.quantity) for movement in movements_for(session, part.part_code)] == [
        (MovementType.IN, 10),
        (MovementType.IN, 5),
    ]


def test_stock_out_decreases_inventory_and_creates_out_movement(session: Session, service: InventoryService) -> None:
    part = create_part(session)
    service.stock_in(part_code=part.part_code, quantity=10, reason="receiving")

    inventory = service.stock_out(part_code=part.part_code, quantity=3, reason="production use")

    assert inventory.quantity == 7
    movements = movements_for(session, part.part_code)
    assert [(movement.movement_type, movement.quantity) for movement in movements] == [
        (MovementType.IN, 10),
        (MovementType.OUT, 3),
    ]


def test_insufficient_stock_rolls_back_without_out_movement(session: Session, service: InventoryService) -> None:
    part = create_part(session)
    service.stock_in(part_code=part.part_code, quantity=2, reason="receiving")

    with pytest.raises(InsufficientInventoryError) as exc_info:
        service.stock_out(part_code=part.part_code, quantity=3, reason="production use")

    assert exc_info.value.available_quantity == 2
    assert session.get(Inventory, part.part_code).quantity == 2
    assert [(movement.movement_type, movement.quantity) for movement in movements_for(session, part.part_code)] == [
        (MovementType.IN, 2)
    ]


@pytest.mark.parametrize("method_name", ["stock_in", "stock_out"])
@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_is_rejected(
    service: InventoryService,
    method_name: str,
    quantity: int,
) -> None:
    with pytest.raises(InvalidInventoryInputError):
        getattr(service, method_name)(part_code="1", quantity=quantity, reason="invalid")


def test_nonexistent_part_is_rejected(service: InventoryService) -> None:
    with pytest.raises(PartNotFoundError):
        service.stock_in(part_code="999", quantity=1, reason="invalid part")


def test_get_by_part_distinguishes_missing_inventory_from_missing_part(
    session: Session,
    service: InventoryService,
) -> None:
    part = create_part(session)

    assert service.get_inventory_by_part_code(part.part_code) is None
    assert service.get_inventory_by_part_code(part.part_code) is None
    with pytest.raises(PartNotFoundError):
        service.get_inventory_by_part_code("999")


def test_get_all_inventory_includes_part_relationship(session: Session, service: InventoryService) -> None:
    part = create_part(session)
    service.stock_in(part_code=part.part_code, quantity=4, reason="receiving")

    inventory = service.get_all_inventory()

    assert len(inventory) == 1
    assert inventory[0].part.part_code == part.part_code
    assert inventory[0].quantity == 4


def test_nonexistent_job_id_is_rejected_without_inventory_change(
    session: Session,
    service: InventoryService,
) -> None:
    part = create_part(session)

    with pytest.raises(JobNotFoundError):
        service.stock_in(part_code=part.part_code, quantity=4, reason="invalid job", job_id=999)

    assert session.get(Inventory, part.part_code) is None
    assert movements_for(session, part.part_code) == []


def test_movement_failure_rolls_back_inventory_change(
    session: Session,
    service: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    part = create_part(session)

    def raise_after_inventory_flush(**kwargs: object) -> InventoryMovement:
        raise RuntimeError("simulated movement write failure")

    monkeypatch.setattr(service, "_create_movement", raise_after_inventory_flush)

    with pytest.raises(RuntimeError, match="movement write failure"):
        service.stock_in(part_code=part.part_code, quantity=10, reason="must rollback")

    session.expire_all()
    assert session.get(Inventory, part.part_code) is None
    assert movements_for(session, part.part_code) == []


def test_adjust_is_explicitly_deferred(service: InventoryService) -> None:
    with pytest.raises(NotImplementedError, match="ADJUST"):
        service.adjust()
