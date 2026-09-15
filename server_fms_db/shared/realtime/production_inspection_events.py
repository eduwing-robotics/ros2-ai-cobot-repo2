"""Best-effort post-commit identity notifications for ProductionInspection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final


PRODUCTION_INSPECTION_CHANGED_CHANNEL: Final = "production.inspection.changed"
ProductionInspectionChangeCallback = Callable[[int], None]

_active_callback: ProductionInspectionChangeCallback | None = None


def set_production_inspection_change_callback(
    callback: ProductionInspectionChangeCallback | None,
) -> None:
    global _active_callback
    _active_callback = callback


def get_production_inspection_change_callback() -> ProductionInspectionChangeCallback | None:
    return _active_callback


def production_inspection_changed_payload(*, inspection_id: int) -> dict[str, str | int]:
    return {
        "inspection_id": inspection_id,
        "changed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
