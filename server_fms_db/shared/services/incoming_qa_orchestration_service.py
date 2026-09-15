"""Durable global Incoming-QA single-flight and readiness policy.

This service never dispatches Vision.  It serializes allocation of immutable
inspection requests and derives readiness exclusively from persisted evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionStatus,
    ProductionJob,
)
from shared.services.material_inspection_service import MaterialInspectionService

# ASCII IQA in a documented signed-bigint-safe namespace.  Never use hash().
INCOMING_QA_GLOBAL_LOCK_KEY = 0x495141


@dataclass(frozen=True, slots=True)
class PreProductionQAReadiness:
    required: bool
    ready: bool
    qa_passed: bool
    test_hold: bool
    total_items: int
    released_items: int


class IncomingQAOrchestrationService:
    """Own durable QA allocation serialization and job-wide readiness."""

    _ACTIVE_STATUSES = (
        MaterialInspectionStatus.REQUESTED,
        MaterialInspectionStatus.RUNNING,
    )

    def __init__(self, session: Session) -> None:
        self._session = session
        self._inspections = MaterialInspectionService()

    def acquire_global_transaction_guard(self) -> None:
        """Serialize new QA allocations across PostgreSQL server processes.

        The lock lasts only for persisted active-state inspection and allocation.
        Vision HTTP is intentionally sent after the transaction commits.
        """
        if self._session.get_bind().dialect.name == "postgresql":
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
                {"key": INCOMING_QA_GLOBAL_LOCK_KEY},
            )

    def get_active_inspection(
        self, *, excluding_delivery_item_id: int | None = None
    ) -> MaterialInspection | None:
        statement = select(MaterialInspection).where(
            MaterialInspection.status.in_(self._ACTIVE_STATUSES)
        ).order_by(MaterialInspection.requested_at, MaterialInspection.inspection_id)
        if excluding_delivery_item_id is not None:
            statement = statement.where(
                MaterialInspection.delivery_item_id != excluding_delivery_item_id
            )
        return self._session.scalar(statement)

    def preproduction_readiness(self, *, job_id: int) -> PreProductionQAReadiness:
        job = self._session.get(ProductionJob, job_id)
        if job is None:
            raise ValueError(f"Production job not found: job_id={job_id}.")
        # DeliveryItems are the expected-set authority. Inspection rows are only
        # evidence for individual expected items; an absent row cannot bypass QA.
        items = list(self._session.scalars(
            select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job_id)
            .order_by(JobMaterialDeliveryItem.delivery_item_id)
        ))
        released = sum(
            1
            for item in items
            if self._inspections.is_delivery_item_released(
                self._session, item.delivery_item_id
            )
        )
        qa_passed = bool(items) and released == len(items)
        return PreProductionQAReadiness(
            required=True,
            # The persisted test hold blocks downstream readiness only. It
            # never rewrites or reinterprets inspection PASS evidence.
            ready=qa_passed and not job.incoming_qa_test_hold,
            qa_passed=qa_passed,
            test_hold=job.incoming_qa_test_hold,
            total_items=len(items),
            released_items=released,
        )

    def is_preproduction_ready(self, *, job_id: int) -> bool:
        return self.preproduction_readiness(job_id=job_id).ready
