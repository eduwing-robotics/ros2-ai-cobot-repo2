"""Explicit boundary mapping from server installation identities to Cell wire slots.

The database Recipe/JobStep ``slot_code`` remains the canonical installation
identity.  This module maps only the Robot Cell's final teaching aliases while
serializing ExecuteTask ``parts_json``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _WireSlotMapping:
    part_code: str
    wire_slot: str


class RobotCellSlotMapper:
    """Map current HOUSE_A/HOUSE_B wall targets without changing DB identities."""

    _MAPPINGS: dict[tuple[str, str], _WireSlotMapping] = {
        ("HOUSE_A", "HOUSE_A_OUTER_WALL_DOOR_01"): _WireSlotMapping("WALL-EXT-DOOR-01", "blue"),
        ("HOUSE_A", "HOUSE_A_OUTER_WALL_LEFT_01"): _WireSlotMapping("WALL-EXT-LEFT-WINDOW-01", "yellow"),
        ("HOUSE_A", "HOUSE_A_OUTER_WALL_REAR_01"): _WireSlotMapping("WALL-EXT-BACK-WINDOW-01", "red"),
        ("HOUSE_A", "HOUSE_A_OUTER_WALL_RIGHT_01"): _WireSlotMapping("WALL-EXT-RIGHT-01", "red_s"),
        ("HOUSE_A", "HOUSE_A_INNER_WALL_DOOR_01"): _WireSlotMapping("WALL-INT-HOUSE-A-DOOR-01", "blue_in"),
        ("HOUSE_A", "HOUSE_A_INNER_WALL_01"): _WireSlotMapping("WALL-INT-HOUSE-A-01", "yellow_in"),
        ("HOUSE_B", "HOUSE_B_OUTER_WALL_DOOR_01"): _WireSlotMapping("WALL-EXT-DOOR-01", "blue"),
        ("HOUSE_B", "HOUSE_B_OUTER_WALL_LEFT_01"): _WireSlotMapping("WALL-EXT-LEFT-WINDOW-01", "yellow"),
        ("HOUSE_B", "HOUSE_B_OUTER_WALL_REAR_01"): _WireSlotMapping("WALL-EXT-BACK-WINDOW-01", "red"),
        ("HOUSE_B", "HOUSE_B_OUTER_WALL_RIGHT_01"): _WireSlotMapping("WALL-EXT-RIGHT-01", "red_s"),
        ("HOUSE_B", "HOUSE_B_INNER_WALL_01"): _WireSlotMapping("WALL-INT-HOUSE-B-01", "red_in"),
    }

    @classmethod
    def map(
        cls,
        *,
        product_code: str,
        canonical_slot_code: str,
        part_code: str,
    ) -> str:
        """Return the Cell wire slot, preserving unmapped canonical slots.

        BASE/ROOF and legacy targets deliberately pass through unchanged.  For a
        current mapped wall target, a mismatched legacy Part preserves the
        canonical slot for historical snapshot compatibility.
        """
        product = product_code.strip() if isinstance(product_code, str) else ""
        canonical = canonical_slot_code.strip() if isinstance(canonical_slot_code, str) else ""
        part = part_code.strip() if isinstance(part_code, str) else ""
        mapping = cls._MAPPINGS.get((product, canonical))
        if mapping is None:
            return canonical
        # Historical snapshots can retain a legacy Part identity at the same
        # canonical installation target.  Preserve their old wire behavior;
        # only the final approved current Part pairing receives an alias.
        if part != mapping.part_code:
            return canonical
        return mapping.wire_slot
