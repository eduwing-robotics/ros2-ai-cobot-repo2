"""One-way durable operator evidence for MANUAL material prestaging."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import JobMaterialDelivery, JobStatus, MaterialDeliveryStatus, ProductionJob, SupplyMode
from shared.realtime.production_events import get_production_change_callback


class ManualPrestageError(RuntimeError):
    """Base error for a MANUAL prestage assertion."""


class ManualPrestageJobNotFoundError(ManualPrestageError):
    pass


class ManualPrestageDeliveryNotFoundError(ManualPrestageError):
    pass


class ManualPrestagePolicyError(ManualPrestageError):
    pass


class ManualPrestageStateError(ManualPrestageError):
    pass


class ManualPrestageService:
    """Record MANUAL at-cell preparation independently of QA and transport.

    This intentionally does not change Delivery.status: MANUAL delivery does not
    represent a TurtleBot lifecycle. The evidence is consumed later by manual
    StepReadiness only after the authoritative Incoming QA release predicate.
    """

    def __init__(self, session: Session, *, post_commit_callback=None) -> None:
        self._session = session
        self._post_commit_callback = post_commit_callback or get_production_change_callback()

    def confirm_manual_prestage_ready(
        self, *, job_id: int, job_delivery_id: int, request_id: str
    ) -> JobMaterialDelivery:
        normalized_request_id = self._normalize_request_id(request_id)
        self._validate_positive_id(job_id, "job_id")
        self._validate_positive_id(job_delivery_id, "job_delivery_id")
        try:
            job = self._session.scalar(
                select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
            )
            if job is None:
                raise ManualPrestageJobNotFoundError(
                    f"Production job not found: job_id={job_id}."
                )
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise ManualPrestageStateError(
                    f"Manual prestage is not allowed for terminal production job status {job.status.value}."
                )
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
                .with_for_update()
            )
            if delivery is None or delivery.production_job_id != job_id:
                raise ManualPrestageDeliveryNotFoundError(
                    "Job material delivery not found for production job."
                )
            self._require_manual_policy_snapshot(delivery)
            if delivery.manual_prestage_ready_at is not None:
                # Preserve original evidence for exact retries and later clicks.
                self._session.commit()
                return delivery
            if delivery.status is not MaterialDeliveryStatus.PENDING:
                raise ManualPrestageStateError(
                    "A first manual-prestage confirmation is allowed only while "
                    f"Delivery is PENDING, got {delivery.status.value}."
                )
            delivery.manual_prestage_ready_at = datetime.now(timezone.utc)
            delivery.manual_prestage_request_id = normalized_request_id
            self._session.commit()
            self._notify_production_changed(job_id, "manual_prestage_ready")
            return delivery
        except Exception:
            self._session.rollback()
            raise
    def _notify_production_changed(self, job_id: int, reason: str) -> None:
        callback = self._post_commit_callback
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:
            logging.getLogger(__name__).exception("Production post-commit notification failed job_id=%s reason=%s", job_id, reason)



    @staticmethod
    def _require_manual_policy_snapshot(delivery: JobMaterialDelivery) -> None:
        group = (
            delivery.supply_group_code.strip()
            if isinstance(delivery.supply_group_code, str)
            else ""
        )
        if delivery.supply_mode is not SupplyMode.MANUAL or not group:
            raise ManualPrestagePolicyError(
                "Manual prestage confirmation requires MANUAL SupplyMode and a "
                "non-empty supply_group_code."
            )

    @staticmethod
    def _normalize_request_id(request_id: str) -> str:
        if not isinstance(request_id, str) or not (normalized := request_id.strip()):
            raise ManualPrestageError("request_id must be a non-empty string.")
        if len(normalized) > 100:
            raise ManualPrestageError("request_id must be 100 characters or fewer.")
        return normalized

    @staticmethod
    def _validate_positive_id(value: int, field: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ManualPrestageError(f"{field} must be a positive integer.")
