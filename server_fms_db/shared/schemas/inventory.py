from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class InventoryResponse(BaseModel):
    part_code: str
    part_name: str
    # Physical on-hand quantity; reservations do not reduce this field.
    quantity: int
    reserved_quantity: int = 0
    available_quantity: int = 0
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class VoiceInventoryProductContext(BaseModel):
    """One active production master that uses a physical inventory Part."""

    product_code: str
    product_name: str


class VoiceInventoryItem(InventoryResponse):
    """Read-only inventory row with master-derived product usage context."""

    products: list[VoiceInventoryProductContext] = Field(default_factory=list)
    # Voice-only master metadata. PrivateAttr intentionally keeps the public
    # AI/inventory structured response contract unchanged.
    _is_option_material: bool = PrivateAttr(default=False)

    @property
    def is_option_material(self) -> bool:
        return self._is_option_material

    def mark_option_material(self, value: bool) -> "VoiceInventoryItem":
        self._is_option_material = value
        return self
