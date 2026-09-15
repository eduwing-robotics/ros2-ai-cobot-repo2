"""Best-effort notifications for committed execution-attempt errors.

The payload identifies an attempt only.  API consumers must re-read PostgreSQL
before constructing a Unity error projection.
"""

from __future__ import annotations

from collections.abc import Callable

from typing import Final


ERROR_EVENT_CHANNEL: Final = "error.event"
ExecutionAttemptErrorCallback = Callable[[int], None]

_active_callback: ExecutionAttemptErrorCallback | None = None


def set_execution_attempt_error_callback(callback: ExecutionAttemptErrorCallback | None) -> None:
    global _active_callback
    _active_callback = callback


def get_execution_attempt_error_callback() -> ExecutionAttemptErrorCallback | None:
    return _active_callback


def error_event_payload(*, attempt_id: int) -> dict[str, int]:
    return {"attempt_id": attempt_id}
