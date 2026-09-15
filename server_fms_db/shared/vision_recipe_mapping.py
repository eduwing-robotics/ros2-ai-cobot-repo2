"""Pure Vision Incoming-QA v0.2 mode/slot/class reference mapping.

Vision recipes own cameras, views, and ROIs.  The FMS only owns the stable
inspection-mode / slot / expected-vision-class contract defined here.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class IncomingQAInspectionMode(StrEnum):
    BASE_AB = "BASE_AB"
    HOUSE_B = "HOUSE_B"
    HOUSE_A = "HOUSE_A"


class VisionRecipeMappingError(ValueError):
    """Raised for a mode/slot pair that is absent from the v0.2 contract."""


_MAPPING = MappingProxyType({
    IncomingQAInspectionMode.BASE_AB: MappingProxyType({
        "C08": "base_house_a",
        "C09": "base_house_b",
    }),
    IncomingQAInspectionMode.HOUSE_B: MappingProxyType({
        "B01": "wall_ext_back_window",
        "B02": "wall_ext_door",
        "B03": "wall_ext_left_window",
        "B04": "wall_ext_right",
        "B05": "wall_int_house_b",
        "B06": "roof_zip",
    }),
    IncomingQAInspectionMode.HOUSE_A: MappingProxyType({
        "A01": "wall_ext_back_window",
        "A02": "wall_ext_door",
        "A03": "wall_ext_left_window",
        "A04": "wall_ext_right",
        "A05": "wall_int_house_a",
        "A06": "roof_nozip",
        "A07": "wall_int_house_a_door",
    }),
})


def allowed_slots(mode: IncomingQAInspectionMode) -> tuple[str, ...]:
    """Return the stable slots in contract order for one inspection mode."""
    return tuple(_MAPPING[IncomingQAInspectionMode(mode)])


def expected_vision_class(*, mode: IncomingQAInspectionMode, slot_id: str) -> str:
    """Look up the authoritative expected class for one mode/slot pair."""
    normalized_slot = slot_id.strip() if isinstance(slot_id, str) else ""
    try:
        return _MAPPING[IncomingQAInspectionMode(mode)][normalized_slot]
    except KeyError as exc:
        raise VisionRecipeMappingError(
            f"Unsupported Vision Incoming-QA slot {normalized_slot!r} for mode {mode.value}."
        ) from exc


def find_mode_slot_for_vision_class(*, mode: IncomingQAInspectionMode, vision_class: str) -> str:
    """Return the unique slot for a class in one mode, or fail closed."""
    normalized_class = vision_class.strip() if isinstance(vision_class, str) else ""
    matches = [slot_id for slot_id, configured in _MAPPING[IncomingQAInspectionMode(mode)].items() if configured == normalized_class]
    if len(matches) != 1:
        raise VisionRecipeMappingError(
            f"Vision class {normalized_class!r} has no unique slot in mode {mode.value}."
        )
    return matches[0]


# Product-to-mode composition is kept beside the wire slot mapping so product
# literals never become distributed lifecycle branches.  A mode can be shared
# (BASE_AB); its selected slots remain product-specific reference data.
_PRODUCT_INITIAL_MODE_SLOTS = MappingProxyType({
    "HOUSE_A": (
        (IncomingQAInspectionMode.BASE_AB, ("C08",)),
        (IncomingQAInspectionMode.HOUSE_A, allowed_slots(IncomingQAInspectionMode.HOUSE_A)),
    ),
    "HOUSE_B": (
        (IncomingQAInspectionMode.BASE_AB, ("C09",)),
        (IncomingQAInspectionMode.HOUSE_B, allowed_slots(IncomingQAInspectionMode.HOUSE_B)),
    ),
})


def initial_mode_slots_for_product(
    product_code: str,
) -> tuple[tuple[IncomingQAInspectionMode, tuple[str, ...]], ...]:
    """Return one product's ordered initial Incoming-QA recipe contract."""

    try:
        return _PRODUCT_INITIAL_MODE_SLOTS[product_code]
    except KeyError as exc:
        raise VisionRecipeMappingError(
            f"Product {product_code!r} has no configured Incoming-QA v0.2 recipe."
        ) from exc


def resolve_product_mode_slot(
    *, product_code: str, vision_class: str
) -> tuple[IncomingQAInspectionMode, str]:
    """Resolve a persisted Part vision class through that product's QA recipe.

    Identical classes in HOUSE_A and HOUSE_B are intentionally disambiguated by
    this single reference mapping boundary, not by orchestration conditionals.
    """

    normalized = vision_class.strip() if isinstance(vision_class, str) else ""
    matches: list[tuple[IncomingQAInspectionMode, str]] = []
    for mode, slots in initial_mode_slots_for_product(product_code):
        for slot_id in slots:
            if expected_vision_class(mode=mode, slot_id=slot_id) == normalized:
                matches.append((mode, slot_id))
    if len(matches) != 1:
        raise VisionRecipeMappingError(
            f"Vision class {normalized!r} has no unique Incoming-QA slot for product {product_code!r}."
        )
    return matches[0]
