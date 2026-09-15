"""Generic linear one-ahead material-prefetch candidate selection."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialDeliveryStatus,
    SupplyMode,
)


@dataclass(frozen=True)
class MaterialPrefetchCandidate:
    job_delivery_id: int
    earliest_consuming_step_order: int


class MaterialPrefetchService:
    """Select one future transported Delivery without knowing product semantics.

    The selector is intentionally only a scheduler hint. Transport eligibility,
    global Incoming QA, DROP ownership, and durable Attempt claiming remain
    authoritative in ``TransportEligibilityService``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def nearest_future_transported_delivery(
        self,
        *,
        job_id: int,
        execution_frontier_step_order: int,
    ) -> MaterialPrefetchCandidate | None:
        delivery_ids = list(
            self._session.scalars(
                select(JobMaterialDelivery.job_delivery_id)
                .where(
                    JobMaterialDelivery.production_job_id == job_id,
                    JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
                    JobMaterialDelivery.status == MaterialDeliveryStatus.PENDING,
                )
                .order_by(JobMaterialDelivery.job_delivery_id)
            )
        )

        candidates: list[MaterialPrefetchCandidate] = []
        for delivery_id in delivery_ids:
            step_orders = self._bound_consuming_step_orders(
                job_id=job_id,
                job_delivery_id=delivery_id,
            )
            # Deferred/null binding cannot prove a consuming execution order.
            # A future transported deferred material must remain fail-closed.
            if not step_orders:
                continue
            earliest = min(step_orders)
            if earliest <= execution_frontier_step_order:
                continue
            candidates.append(
                MaterialPrefetchCandidate(
                    job_delivery_id=delivery_id,
                    earliest_consuming_step_order=earliest,
                )
            )

        return min(
            candidates,
            key=lambda candidate: (
                candidate.earliest_consuming_step_order,
                candidate.job_delivery_id,
            ),
            default=None,
        )

    def _bound_consuming_step_orders(
        self,
        *,
        job_id: int,
        job_delivery_id: int,
    ) -> list[int]:
        items = list(
            self._session.scalars(
                select(JobMaterialDeliveryItem).where(
                    JobMaterialDeliveryItem.job_delivery_id == job_delivery_id
                )
            )
        )
        if not items or any(item.job_step_id is None for item in items):
            return []

        step_ids = {item.job_step_id for item in items}
        steps = list(
            self._session.scalars(
                select(JobStep).where(
                    JobStep.job_id == job_id,
                    JobStep.job_step_id.in_(step_ids),
                )
            )
        )
        if len(steps) != len(step_ids):
            return []
        return [step.step_order for step in steps]
