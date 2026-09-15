"""Durable, UI-neutral confirmation of material physical readiness."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import JobMaterialDelivery, JobStatus, MaterialDeliveryStatus, ProductionJob, SupplyMode


class PhysicalReadyError(RuntimeError):
    """Base error for one-way material physical-ready confirmation."""


class PhysicalReadyDeliveryNotFoundError(PhysicalReadyError):
    pass


class PhysicalReadyPolicyError(PhysicalReadyError):
    pass


class PhysicalReadyStateError(PhysicalReadyError):
    pass


class PhysicalReadyService:
    """Record physical preparation independently from QA and transport state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def confirm_physical_ready(
        self, *, job_delivery_id: int, request_id: str
    ) -> JobMaterialDelivery:
        """Confirm a configured group once; exact and equivalent retries are no-ops.

        The caller-facing service owns one transaction.  It does not inspect QA,
        mutate delivery/feed status, or make a JobStep dispatchable in Phase 1.
        """

        normalized_request_id = self._normalize_request_id(request_id)
        self._validate_positive_id(job_delivery_id)
        try:
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
                .with_for_update()
            )
            if delivery is None:
                raise PhysicalReadyDeliveryNotFoundError(
                    f"Job material delivery not found: job_delivery_id={job_delivery_id}."
                )
            job = self._session.scalar(
                select(ProductionJob)
                .where(ProductionJob.job_id == delivery.production_job_id)
                .with_for_update()
            )
            if job is None:
                raise PhysicalReadyDeliveryNotFoundError(
                    f"Production job not found for job_delivery_id={job_delivery_id}."
                )
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise PhysicalReadyStateError(
                    f"Physical-ready confirmation is not allowed for terminal production job status {job.status.value}."
                )
            self._require_new_policy_snapshot(delivery)
            if delivery.physical_ready_at is not None:
                # Preserve original evidence even when a user re-clicks under a
                # different request id.  The state is already truthfully ready.
                self._session.commit()
                return delivery
            if delivery.status is not MaterialDeliveryStatus.PENDING:
                raise PhysicalReadyStateError(
                    "A first physical-ready confirmation is allowed only while "
                    f"Delivery is PENDING, got {delivery.status.value}."
                )
            delivery.physical_ready_at = datetime.now(timezone.utc)
            delivery.physical_ready_request_id = normalized_request_id
            self._session.commit()
            return delivery
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _require_new_policy_snapshot(delivery: JobMaterialDelivery) -> None:
        mode = delivery.supply_mode
        group = delivery.supply_group_code.strip() if isinstance(delivery.supply_group_code, str) else ""
        if mode is not SupplyMode.TRANSPORTED or not group:
            raise PhysicalReadyPolicyError(
                "Physical-ready confirmation requires TRANSPORTED SupplyMode and a "
                "non-empty supply_group_code; MANUAL and legacy Deliveries are not eligible."
            )

    @staticmethod
    def _normalize_request_id(request_id: str) -> str:
        if not isinstance(request_id, str) or not (normalized := request_id.strip()):
            raise PhysicalReadyError("request_id must be a non-empty string.")
        if len(normalized) > 100:
            raise PhysicalReadyError("request_id must be 100 characters or fewer.")
        return normalized

    @staticmethod
    def _validate_positive_id(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PhysicalReadyError("job_delivery_id must be a positive integer.")
