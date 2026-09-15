"""Durable Material Delivery -> MATERIAL_FEED runtime lifecycle."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialFeedExecution,
    MaterialDeliveryStatus,
    MaterialFeedStatus,
)


class MaterialFeedExecutionError(RuntimeError):
    """Base feed runtime error."""


class MaterialFeedExecutionNotFoundError(MaterialFeedExecutionError):
    pass


class InvalidMaterialFeedStateTransitionError(MaterialFeedExecutionError):
    pass


class MaterialFeedEligibilityError(MaterialFeedExecutionError):
    pass


class MaterialFeedExecutionService:
    """Own Feed lifecycle separately from delivery and Assembly JobSteps."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def instantiate_for_deliveries(self, deliveries: list[JobMaterialDelivery]) -> list[JobMaterialFeedExecution]:
        """Create one PENDING Feed runtime per snapshot delivery in the caller transaction."""
        executions = [
            JobMaterialFeedExecution(
                job_delivery_id=delivery.job_delivery_id,
                status=MaterialFeedStatus.PENDING,
                completed_json="[]",
            )
            for delivery in deliveries
        ]
        self._session.add_all(executions)
        self._session.flush()
        return executions

    def get_for_delivery(self, job_delivery_id: int) -> JobMaterialFeedExecution | None:
        return self._session.scalar(
            select(JobMaterialFeedExecution).where(
                JobMaterialFeedExecution.job_delivery_id == job_delivery_id
            )
        )

    def get_for_job(self, job_id: int) -> list[JobMaterialFeedExecution]:
        return list(
            self._session.scalars(
                select(JobMaterialFeedExecution)
                .join(JobMaterialDelivery)
                .where(JobMaterialDelivery.production_job_id == job_id)
                .order_by(JobMaterialDelivery.batch_order)
            )
        )

    def get_next_eligible(self, *, job_id: int) -> JobMaterialFeedExecution | None:
        return self._session.scalar(
            select(JobMaterialFeedExecution)
            .join(JobMaterialDelivery)
            .where(
                JobMaterialDelivery.production_job_id == job_id,
                JobMaterialDelivery.status == MaterialDeliveryStatus.COMPLETED,
                JobMaterialFeedExecution.status == MaterialFeedStatus.PENDING,
            )
            .order_by(JobMaterialDelivery.batch_order)
        )

    def start_feed(self, feed_execution_id: int) -> JobMaterialFeedExecution:
        def operation() -> JobMaterialFeedExecution:
            feed = self._locked(feed_execution_id)
            if feed.status is not MaterialFeedStatus.PENDING:
                raise InvalidMaterialFeedStateTransitionError(
                    f"Invalid material feed transition: {feed.status.value} -> RUNNING."
                )
            if feed.job_delivery.status is not MaterialDeliveryStatus.COMPLETED:
                raise MaterialFeedEligibilityError(
                    "MATERIAL_FEED requires linked JobMaterialDelivery status COMPLETED."
                )
            feed.status = MaterialFeedStatus.RUNNING
            feed.started_at = self._utcnow()
            self._session.flush()
            return feed
        return self._write(operation)

    def complete_feed(self, feed_execution_id: int, *, completed_slots: tuple[str, ...]) -> JobMaterialFeedExecution:
        def operation() -> JobMaterialFeedExecution:
            feed = self._locked(feed_execution_id)
            self._require_running(feed, target="COMPLETED")
            feed.status = MaterialFeedStatus.COMPLETED
            feed.completed_at = self._utcnow()
            feed.failed_at = None
            feed.error_code = None
            feed.failure_reason = None
            feed.completed_json = json.dumps(list(completed_slots), separators=(",", ":"))
            self._session.flush()
            return feed
        return self._write(operation)

    def fail_feed(
        self, feed_execution_id: int, *, error_code: str | None, failure_reason: str | None, completed_slots: tuple[str, ...]
    ) -> JobMaterialFeedExecution:
        def operation() -> JobMaterialFeedExecution:
            feed = self._locked(feed_execution_id)
            self._require_running(feed, target="FAILED")
            feed.status = MaterialFeedStatus.FAILED
            feed.failed_at = self._utcnow()
            feed.completed_at = None
            feed.error_code = error_code.strip() if error_code else None
            feed.failure_reason = failure_reason.strip() if failure_reason else None
            feed.completed_json = json.dumps(list(completed_slots), separators=(",", ":"))
            self._session.flush()
            return feed
        return self._write(operation)

    def _locked(self, feed_execution_id: int) -> JobMaterialFeedExecution:
        feed = self._session.scalar(
            select(JobMaterialFeedExecution)
            .where(JobMaterialFeedExecution.feed_execution_id == feed_execution_id)
            .with_for_update()
        )
        if feed is None:
            raise MaterialFeedExecutionNotFoundError(
                f"Job material feed execution not found: feed_execution_id={feed_execution_id}."
            )
        return feed

    @staticmethod
    def _require_running(feed: JobMaterialFeedExecution, *, target: str) -> None:
        if feed.status is not MaterialFeedStatus.RUNNING:
            raise InvalidMaterialFeedStateTransitionError(
                f"Invalid material feed transition: {feed.status.value} -> {target}."
            )

    def _write(self, operation):
        try:
            result = operation()
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
