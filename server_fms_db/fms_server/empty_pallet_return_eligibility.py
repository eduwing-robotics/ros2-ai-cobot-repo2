"""Server-owned consumption proof for an empty-pallet return.

Robot Cell SUCCESS proves only the parts in that individual Goal completed.
This helper deliberately uses durable DeliveryItem -> JobStep traceability to
prove that every server-required consumer of one transported Delivery finished.
It does not inspect ``completed_json`` and does not infer pallet inventory.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.empty_pallet_return_service import EmptyPalletReturnNotEligibleError
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialDeliveryStatus,
    StepStatus,
    SupplyMode,
)


class EmptyPalletReturnEligibilityService:
    """Validate a completed transported Delivery's consuming Step evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def assert_eligible(
        self,
        *,
        job_delivery_id: int,
        production_job_id: int | None = None,
        lock_rows: bool = True,
    ) -> JobMaterialDelivery:
        delivery_query = select(JobMaterialDelivery).where(
            JobMaterialDelivery.job_delivery_id == job_delivery_id
        )
        if lock_rows:
            delivery_query = delivery_query.with_for_update()
        delivery = self._session.scalar(delivery_query)
        if delivery is None or (
            production_job_id is not None
            and delivery.production_job_id != production_job_id
        ):
            raise EmptyPalletReturnNotEligibleError(
                "Job material delivery not found for production job."
            )
        if delivery.supply_mode is not SupplyMode.TRANSPORTED:
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return is only applicable to TRANSPORTED Deliveries."
            )
        if delivery.status is not MaterialDeliveryStatus.COMPLETED:
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return requires an already COMPLETED material Delivery."
            )

        items_query = select(JobMaterialDeliveryItem).where(
            JobMaterialDeliveryItem.job_delivery_id == job_delivery_id
        )
        if lock_rows:
            items_query = items_query.with_for_update()
        items = list(self._session.scalars(items_query))
        if not items:
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return requires at least one consuming DeliveryItem."
            )

        step_ids = {item.job_step_id for item in items}
        if None in step_ids:
            # A deferred material item is not evidence of consumption. Roof is
            # MANUAL today, but a TRANSPORTED deferred item must fail closed.
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return requires every DeliveryItem to be bound to a JobStep."
            )

        steps_query = select(JobStep).where(
            JobStep.job_id == delivery.production_job_id,
            JobStep.job_step_id.in_(step_ids),
        )
        if lock_rows:
            steps_query = steps_query.with_for_update()
        steps = list(self._session.scalars(steps_query))
        if len(steps) != len(step_ids) or any(
            step.status is not StepStatus.COMPLETED for step in steps
        ):
            raise EmptyPalletReturnNotEligibleError(
                "Empty-pallet return requires all consuming JobSteps to be COMPLETED."
            )
        return delivery
