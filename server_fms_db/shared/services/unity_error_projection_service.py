"""Read-only normalization of authoritative execution-attempt errors for Unity."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import ExecutionAttempt, ExecutionAttemptStatus


class UnityErrorProjectionService:
    """Project only attempt states whose error meaning is currently authoritative."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_error_event(self, *, attempt_id: int) -> dict[str, Any] | None:
        attempt = self._session.get(ExecutionAttempt, attempt_id)
        if attempt is None or attempt.status not in {
            ExecutionAttemptStatus.UNKNOWN,
            ExecutionAttemptStatus.FAILED,
        }:
            return None
        return self._to_unity_error(attempt)

    def get_active_errors(self) -> list[dict[str, Any]]:
        attempts = self._session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.status == ExecutionAttemptStatus.UNKNOWN)
            .order_by(ExecutionAttempt.attempt_id.asc())
        ).all()
        return [self._to_unity_error(attempt) for attempt in attempts]

    @staticmethod
    def _to_unity_error(attempt: ExecutionAttempt) -> dict[str, Any]:
        if attempt.status == ExecutionAttemptStatus.UNKNOWN:
            detail = attempt.detail or "Execution outcome is undetermined."
        else:
            detail = attempt.detail or "Execution attempt failed."
        return {
            "source": attempt.executor_type.value,
            "job_id": attempt.job_id,
            "step_id": attempt.job_step_id,
            "delivery_id": attempt.job_delivery_id,
            "severity": None,
            "error_code": attempt.error_code,
            "detail": detail,
            "recoverable": None,
        }
