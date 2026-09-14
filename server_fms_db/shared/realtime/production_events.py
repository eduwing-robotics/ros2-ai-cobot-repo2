"""Best-effort production-change notifications for realtime consumers.

PostgreSQL remains the production source of truth. These messages intentionally
contain only identity and observability metadata; consumers must re-read the
authoritative row after receiving one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final


PRODUCTION_CHANGED_CHANNEL: Final = "production.changed"
ProductionChangeCallback = Callable[[int, str | None], None]


_active_callback: ProductionChangeCallback | None = None


def set_production_change_callback(callback: ProductionChangeCallback | None) -> None:
    """Set the process-local FMS post-commit notification sink."""

    global _active_callback
    _active_callback = callback


def get_production_change_callback() -> ProductionChangeCallback | None:
    return _active_callback

def production_changed_payload(*, job_id: int, reason: str | None = None) -> dict[str, str | int]:
    """Build the deliberately minimal, non-authoritative Pub/Sub payload."""

    payload: dict[str, str | int] = {
        "job_id": job_id,
        "changed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    if reason:
        payload["reason"] = reason
    return payload
