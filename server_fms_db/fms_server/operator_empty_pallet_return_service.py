"""Operator-confirmed entry point for a safe explicit empty-pallet return.

This boundary verifies consumption completion, then delegates DROP ownership,
duplicate protection, durable Attempt creation and dispatch to the existing
``EmptyPalletReturnService``. It never infers pallet emptiness from a Robot
Cell result and it never mutates Delivery state directly.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from fms_server.empty_pallet_return_service import (
    EmptyPalletReturnNotEligibleError,
    EmptyPalletReturnService,
)
from fms_server.forklift_action_adapter import ForkliftExecutionResult
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator


class OperatorEmptyPalletReturnService:
    """Require explicit operator use only after every delivery consumer completed."""

    def __init__(
        self,
        session: Session,
        *,
        forklift_execution_coordinator: ForkliftExecutionCoordinator,
    ) -> None:
        self._session = session
        self._empty_return_service = EmptyPalletReturnService(
            session,
            forklift_execution_coordinator=forklift_execution_coordinator,
        )

    def execute_confirmed_empty_return(
        self, *, production_job_id: int, job_delivery_id: int
    ) -> ForkliftExecutionResult:
        """Dispatch one cleanup command only for the owning, fully consumed Delivery."""
        try:
            return self._empty_return_service.execute_empty_pallet_return(
                job_delivery_id=job_delivery_id,
                production_job_id=production_job_id,
                require_consumption_evidence=True,
            )
        except Exception:
            self._session.rollback()
            raise
