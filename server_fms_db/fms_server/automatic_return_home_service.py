"""Durable, fake-runtime-only ReturnHome reconciliation for completed logistics cycles.

ReturnHome is a separate TurtleBot movement.  It is intentionally derived from
committed Delivery, ExecutionAttempt, and DROP evidence rather than any worker
callback, so the decision survives a restart after the final empty return.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    MaterialDeliveryStatus,
    ProductionJob,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    DropResourceService,
    DropResourceState,
)
from shared.services.forklift_movement_guard import (
    RETURN_HOME_COMMAND_TYPE,
    has_active_forklift_movement,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReturnHomeClaim:
    job_id: int
    request_id: str
    attempt_id: int


class AutomaticReturnHomeService:
    """Dispatch at most one durable, job-scoped ReturnHome movement.

    A failed, canceled, or unknown ReturnHome remains history and is never
    automatically replayed: DB state alone cannot prove TurtleBot location.
    """

    def __init__(
        self,
        session: Session,
        *,
        forklift_execution_coordinator_factory: Callable[[Session], ForkliftExecutionCoordinator],
        runtime_enabled: bool,
    ) -> None:
        self._session = session
        self._forklift_execution_coordinator_factory = forklift_execution_coordinator_factory
        self._runtime_enabled = runtime_enabled
        self._drop_resource_service = DropResourceService(session)

    def dispatch_one_eligible_return_home(self) -> bool:
        """Reconcile durable logistics state and dispatch one fake-safe HOME move."""
        if not self._runtime_enabled:
            logger.info(
                "Automatic ReturnHome is disabled because a real TurtleBot "
                "ReturnHome runtime is unavailable."
            )
            return False

        job_ids = list(
            self._session.scalars(
                select(JobMaterialDelivery.production_job_id)
                .where(JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED)
                .distinct()
                .order_by(JobMaterialDelivery.production_job_id)
            )
        )
        for job_id in job_ids:
            claim = self._claim_if_eligible(job_id=job_id)
            if claim is None:
                continue
            try:
                self._forklift_execution_coordinator_factory(self._session).return_home(
                    req_id=claim.request_id,
                    job_id=claim.job_id,
                    attempt_preclaimed=True,
                )
            except Exception:
                # The preclaimed DISPATCHING Attempt is durable and therefore
                # blocks an unsafe automatic replay until an explicit recovery
                # policy exists.
                self._session.rollback()
                logger.exception(
                    "Automatic ReturnHome dispatch failed job_id=%s req_id=%s",
                    claim.job_id,
                    claim.request_id,
                )
                return False
            return True
        return False

    def _claim_if_eligible(self, *, job_id: int) -> ReturnHomeClaim | None:
        """Atomically claim HOME only after all transported Delivery cycles end."""
        try:
            # Reuse DROP's PostgreSQL transaction-scoped serialization point so
            # a material claim cannot slip between FREE validation and the HOME
            # Attempt claim. No action I/O occurs while it is held.
            self._drop_resource_service.acquire_drop_transaction_guard()
            job = self._session.scalar(
                select(ProductionJob)
                .where(ProductionJob.job_id == job_id)
                .with_for_update()
            )
            if job is None or not self._is_eligible_locked(job_id=job_id):
                self._session.rollback()
                return None

            request_id = str(uuid.uuid4())
            payload = {"job_id": job_id}
            attempt = ExecutionAttempt(
                req_id=request_id,
                executor_type=ExecutorType.FORKLIFT,
                command_type=RETURN_HOME_COMMAND_TYPE,
                job_id=job_id,
                attempt_no=(
                    self._session.scalar(
                        select(func.max(ExecutionAttempt.attempt_no)).where(
                            ExecutionAttempt.job_id == job_id,
                            ExecutionAttempt.command_type == RETURN_HOME_COMMAND_TYPE,
                        )
                    )
                    or 0
                ) + 1,
                status=ExecutionAttemptStatus.CREATED,
                request_payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                created_at=datetime.now(timezone.utc),
            )
            self._session.add(attempt)
            self._session.flush()
            self._session.commit()
            return ReturnHomeClaim(
                job_id=job_id,
                request_id=request_id,
                attempt_id=attempt.attempt_id,
            )
        except Exception:
            self._session.rollback()
            raise

    def _is_eligible_locked(self, *, job_id: int) -> bool:
        deliveries = list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .where(
                    JobMaterialDelivery.production_job_id == job_id,
                    JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
                )
                .with_for_update()
            )
        )
        if not deliveries:
            return False
        if any(delivery.status is not MaterialDeliveryStatus.COMPLETED for delivery in deliveries):
            return False
        if self._has_return_home_history(job_id=job_id):
            return False
        if self._has_active_forklift_movement():
            return False
        if self._drop_resource_service.get_drop_state().state is not DropResourceState.FREE:
            return False
        return all(
            self._delivery_command_succeeded(
                job_delivery_id=delivery.job_delivery_id,
                command_type=MATERIAL_TRANSPORT_COMMAND_TYPE,
            )
            and self._delivery_command_succeeded(
                job_delivery_id=delivery.job_delivery_id,
                command_type=EMPTY_RETURN_COMMAND_TYPE,
            )
            for delivery in deliveries
        )

    def _has_return_home_history(self, *, job_id: int) -> bool:
        # Any prior terminal or ambiguous HOME command is deliberately a
        # no-retry boundary. A later manual recovery policy can decide otherwise.
        return self._session.scalar(
            select(ExecutionAttempt.attempt_id)
            .where(
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type == RETURN_HOME_COMMAND_TYPE,
                ExecutionAttempt.job_id == job_id,
            )
            .with_for_update()
            .limit(1)
        ) is not None

    def _has_active_forklift_movement(self) -> bool:
        return has_active_forklift_movement(self._session, for_update=True)

    def _delivery_command_succeeded(self, *, job_delivery_id: int, command_type: str) -> bool:
        return self._session.scalar(
            select(ExecutionAttempt.attempt_id)
            .where(
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.job_delivery_id == job_delivery_id,
                ExecutionAttempt.command_type == command_type,
                ExecutionAttempt.status == ExecutionAttemptStatus.SUCCEEDED,
            )
            .limit(1)
        ) is not None
