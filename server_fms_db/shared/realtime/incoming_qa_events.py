"""Best-effort notifications for committed Incoming QA v0.2 changes.

Redis carries only an identity trigger.  Consumers must re-read the persisted
transaction before projecting any client-facing state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final


INCOMING_QA_CHANGED_CHANNEL: Final = "production.incoming_qa.changed"
IncomingQAChangeCallback = Callable[[int], None]

_active_callback: IncomingQAChangeCallback | None = None


def set_incoming_qa_change_callback(callback: IncomingQAChangeCallback | None) -> None:
    """Set this process's post-commit notification sink."""

    global _active_callback
    _active_callback = callback


def get_incoming_qa_change_callback() -> IncomingQAChangeCallback | None:
    return _active_callback


def incoming_qa_changed_payload(*, transaction_id: int) -> dict[str, str | int]:
    return {
        "transaction_id": transaction_id,
        "changed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
