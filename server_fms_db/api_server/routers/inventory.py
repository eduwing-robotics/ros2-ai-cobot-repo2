from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.database import get_session_factory
from shared.models.factory import Part
from shared.schemas.inventory import InventoryResponse
from shared.services.inventory_service import (
    InventoryService,
    InvalidInventoryInputError,
    PartNotFoundError,
)

router = APIRouter(prefix="/inventory", tags=["Inventory"])


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    with factory() as session:
        yield session


def get_inventory_service(db: Session = Depends(get_db)) -> InventoryService:
    return InventoryService(db)


@router.get("", response_model=list[InventoryResponse])
def get_all_inventory(
    service: Annotated[InventoryService, Depends(get_inventory_service)]
) -> list[InventoryResponse]:
    """Return all existing inventory rows."""
    inventories = service.get_all_inventory()

    responses = []
    for inv in inventories:
        responses.append(
            InventoryResponse(
                part_code=inv.part.part_code,
                part_name=inv.part.part_name,
                quantity=inv.quantity,
                reserved_quantity=inv.reserved_quantity,
                available_quantity=inv.available_quantity,
                updated_at=inv.updated_at,
            )
        )
    return responses


@router.get("/{part_code}", response_model=InventoryResponse)
def get_inventory_by_part_code(
    part_code: str,
    db: Annotated[Session, Depends(get_db)],
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> InventoryResponse:
    """Return inventory for a specific part by its code."""
    try:
        inventory = service.get_inventory_by_part_code(part_code)
    except PartNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidInventoryInputError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if inventory is not None:
        return InventoryResponse(

            part_code=inventory.part.part_code,
            part_name=inventory.part.part_name,
            quantity=inventory.quantity,
            reserved_quantity=inventory.reserved_quantity,
            available_quantity=inventory.available_quantity,
            updated_at=inventory.updated_at,
        )
    else:
        # Part exists but has no inventory row yet.
        # We need to fetch the part directly to get its details.
        normalized_code = part_code.strip()
        part = db.scalar(select(Part).where(Part.part_code == normalized_code))
        if part is None:
            # Fallback (should not happen if InventoryService acts consistently)
            raise HTTPException(status_code=404, detail=f"Part not found: part_code='{normalized_code}'")

        return InventoryResponse(
            part_code=part.part_code,
            part_name=part.part_name,
            quantity=0,
            reserved_quantity=0,
            available_quantity=0,
            updated_at=None,
        )
