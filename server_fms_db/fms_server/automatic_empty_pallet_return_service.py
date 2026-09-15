"""Durable, fake-runtime-only automatic empty-pallet return trigger.

The trigger is intentionally separate from Robot Cell result processing. It is
called only after a Step completion commit and can be rerun on later Worker
ticks, so a process crash cannot lose the cleanup decision. Movement itself
remains exclusively owned by ``EmptyPalletReturnService``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.empty_pallet_return_service import (
    EmptyPalletReturnDuplicateError,
    EmptyPalletReturnError,
    EmptyPalletReturnService,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.models.factory import (
    JobMaterialDelivery,
    JobStatus,
    ProductionJob,
    MaterialDeliveryStatus,
    SupplyMode,
)

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELED,
)


class AutomaticEmptyPalletReturnService:
    """Dispatch at most one previously-unreturned, fully consumed Delivery.

    A prior failed/canceled return is intentionally not auto-retried. Its
    physical position remains uncertain and is left to the existing recovery
    or operator fallback boundary. Existing EmptyPalletReturnService locking
    remains the final duplicate/race guard for automatic and operator calls.
    """

    def __init__(
        self,
        session: Session,
        *,
        forklift_execution_coordinator_factory: Callable[
            [Session], ForkliftExecutionCoordinator
        ],
        runtime_enabled: bool,
    ) -> None:
        self._session = session
        self._forklift_execution_coordinator_factory = (
            forklift_execution_coordinator_factory
        )
        self._runtime_enabled = runtime_enabled

    def dispatch_one_eligible_return(self) -> bool:
        """Evaluate durable state and dispatch one return only in fake mode."""
        if not self._runtime_enabled:
            logger.info(
                "Automatic empty-pallet return is disabled because a real "
                "TurtleBot return runtime is unavailable."
            )
            return False

        delivery_ids = list(self._session.scalars(self._candidate_delivery_ids_query()))
        for delivery_id in delivery_ids:
            if self._dispatch_if_eligible(job_delivery_id=delivery_id):
                return True
        return False

    def _dispatch_if_eligible(self, *, job_delivery_id: int) -> bool:
        try:
            result = EmptyPalletReturnService(
                self._session,
                forklift_execution_coordinator=self._forklift_execution_coordinator_factory(
                    self._session
                ),
            ).execute_empty_pallet_return(
                job_delivery_id=job_delivery_id,
                require_consumption_evidence=True,
                disallow_existing_return_history=True,
            )
            return result.status.value == "SUCCEEDED"
        except (EmptyPalletReturnDuplicateError, EmptyPalletReturnError) as exc:
            # A different operator/worker can own the return. It is not an
            # installation failure and must not mutate the completed Step.
            self._session.rollback()
            logger.info(
                "Automatic empty-pallet return not dispatched delivery_id=%s: %s",
                job_delivery_id,
                exc,
            )
            return False
        except Exception:
            self._session.rollback()
            logger.exception(
                "Automatic empty-pallet return evaluation failed delivery_id=%s",
                job_delivery_id,
            )
            return False

    @staticmethod
    def _candidate_delivery_ids_query():
        """Select only completed transport deliveries on live parent Jobs."""
        return (
            select(JobMaterialDelivery.job_delivery_id)
            .join(
                ProductionJob,
                ProductionJob.job_id == JobMaterialDelivery.production_job_id,
            )
            .where(
                JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
                JobMaterialDelivery.status == MaterialDeliveryStatus.COMPLETED,
                ProductionJob.status.not_in(_TERMINAL_JOB_STATUSES),
            )
            .order_by(JobMaterialDelivery.job_delivery_id)
        )
