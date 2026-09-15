"""Read-only PostgreSQL source for Unity's initial production snapshot."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotService
from shared.services.unity_error_projection_service import UnityErrorProjectionService
from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotJobNotFoundError
from shared.models.factory import JobStatus, ProductionJob
from shared.services.unity_current_stage_projection_service import UnityCurrentStageProjectionService
from api_server.services.unity_incoming_qa_projection_service import UnityIncomingQAProjectionService
from api_server.services.unity_production_inspection_projection_service import UnityProductionInspectionProjectionService

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED})
SEOUL = ZoneInfo("Asia/Seoul")


class ProductionSnapshotService:
    """Build a bounded, read-only production overview from PostgreSQL."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_snapshot(self, *, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        """Return only durable Job data; telemetry and live errors are external."""

        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        today_start = datetime.combine(current.astimezone(SEOUL).date(), time.min, tzinfo=SEOUL)
        with self._session_factory() as session:
            statement = (
                select(ProductionJob)
                .options(selectinload(ProductionJob.product))
                .where(
                    or_(
                        ProductionJob.status.not_in(TERMINAL_JOB_STATUSES),
                        and_(
                            ProductionJob.status.in_((JobStatus.COMPLETED, JobStatus.CANCELED)),
                            ProductionJob.completed_at >= today_start,
                        ),
                        and_(
                            ProductionJob.status == JobStatus.FAILED,
                            ProductionJob.failed_at >= today_start,
                        ),
                    )
                )
                .order_by(ProductionJob.requested_at.desc(), ProductionJob.job_id.desc())
            )
            jobs = list(session.scalars(statement))

            job_dicts = [self._to_projected_job(session=session, job=job) for job in jobs]
            job_ids = [job.job_id for job in jobs]
            incoming_qa = UnityIncomingQAProjectionService(session).get_snapshots(job_ids=job_ids)
            production_inspections = UnityProductionInspectionProjectionService(session).get_snapshots(job_ids=job_ids)
            active_errors = UnityErrorProjectionService(session).get_active_errors()

        return {
            "jobs": job_dicts,
            "robots": [],
            "transports": [],
            "incoming_qa": incoming_qa,
            "production_inspections": production_inspections,
            "active_errors": active_errors,
        }

    def get_job_status(self, job_id: int) -> dict[str, Any] | None:
        """Read one current Job from PostgreSQL for a realtime notification."""

        if not isinstance(job_id, int) or job_id < 1:
            return None
        with self._session_factory() as session:
            job = session.scalar(
                select(ProductionJob)
                .options(selectinload(ProductionJob.product))
                .where(ProductionJob.job_id == job_id)
            )
            if job is None:
                return None

            return self._to_projected_job(session=session, job=job)

    def get_incoming_qa_status(self, transaction_id: int) -> dict[str, Any] | None:
        """Read one canonical transaction after a Redis identity trigger."""

        if not isinstance(transaction_id, int) or transaction_id < 1:
            return None
        with self._session_factory() as session:
            return UnityIncomingQAProjectionService(session).get_transaction_status(
                transaction_id=transaction_id
            )

    def get_production_inspection_status(self, inspection_id: int) -> dict[str, Any] | None:
        """Fresh-session canonical latest-cycle projection for a Redis identity trigger."""
        if not isinstance(inspection_id, int) or inspection_id < 1:
            return None
        with self._session_factory() as session:
            return UnityProductionInspectionProjectionService(session).get_inspection_status(
                inspection_id=inspection_id
            )

    def get_error_event(self, attempt_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            return UnityErrorProjectionService(session).get_error_event(attempt_id=attempt_id)




    def _to_projected_job(self, *, session: Session, job: ProductionJob) -> dict[str, Any]:
        """Use one stage projection for both reconnect snapshot and live status."""
        result = self._to_job(job)
        result["current_stage_code"] = UnityCurrentStageProjectionService(session).derive(job=job)
        process_stage = UnityCurrentStageProjectionService(session).derive_process_stage(job=job)
        result.update(process_stage or {
            "process_stage_code": None,
            "process_stage_order": None,
            "process_stage_display_name": None,
        })
        try:
            snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=job.job_id)
            result["current_step"] = snapshot.current_step.model_dump(mode="json") if snapshot.current_step else None
            result["next_step"] = snapshot.next_step.model_dump(mode="json") if snapshot.next_step else None
        except ProductionExecutionSnapshotJobNotFoundError:
            result["current_step"] = None
            result["next_step"] = None
        return result

    @staticmethod
    def _to_job(job: ProductionJob) -> dict[str, Any]:
        """Expose only existing Job fields; no priority or derived lifecycle state."""

        return {
            "job_id": job.job_id,
            "job_code": job.job_code,
            "product_code": job.product.product_code if job.product is not None else None,
            "status": job.status.value,
            "control_state": job.control_state.value,
            "roof_option_code": job.roof_option_code.value if job.roof_option_code is not None else None,
            "requested_at": _timestamp(job.requested_at),
            "started_at": _timestamp(job.started_at),
            "completed_at": _timestamp(job.completed_at),
        }


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
