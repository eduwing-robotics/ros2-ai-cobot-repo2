"""Durable reservation and production-issue operations for Inventory.

This service deliberately does not own a transaction boundary.  Job creation,
terminal Job transitions, and the accepted Robot Cell execution transition call
it inside their existing authoritative transactions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    Inventory,
    InventoryMovement,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    ExecutionAttempt,
    ExecutorType,
    MovementType,
    Part,
    ProductionJob,
)


class InventoryReservationError(RuntimeError):
    """Base inventory-reservation lifecycle error."""


class InsufficientAvailableInventoryError(InventoryReservationError):
    def __init__(self, *, part_code: str, requested_quantity: int, available_quantity: int) -> None:
        self.part_code = part_code
        self.requested_quantity = requested_quantity
        self.available_quantity = available_quantity
        super().__init__(
            f"Insufficient available inventory for part_code={part_code}: "
            f"requested={requested_quantity}, available={available_quantity}."
        )


class InventoryReservationInvariantError(InventoryReservationError):
    """Raised instead of silently repairing missing or inconsistent allocations."""


class InventoryReservationService:
    """Reserve full immutable Job requirements, issue one Step, and release unused stock."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve_job_requirements(self, *, job: ProductionJob) -> None:
        """Reserve every persisted Job-local material requirement exactly once at creation.

        Call only after initial and deferred/gated DeliveryItems are materialized
        in the caller's still-open Job creation transaction.
        """
        requirements = self._job_requirements(job_id=job.job_id)
        if not requirements:
            return
        inventories = self._locked_inventory_rows(requirements)
        for part_code, quantity in requirements.items():
            inventory = inventories.get(part_code)
            available = inventory.available_quantity if inventory is not None else 0
            if inventory is None or available < quantity:
                raise InsufficientAvailableInventoryError(
                    part_code=part_code,
                    requested_quantity=quantity,
                    available_quantity=available,
                )
        for part_code, quantity in requirements.items():
            inventories[part_code].reserved_quantity += quantity
        self._session.flush()

    def consume_step_after_execution_accepted(self, *, job_step_id: int) -> bool:
        """Issue a Step's immutable delivery items after durable Cell Goal acceptance.

        The durable ``inventory_consumed_at`` marker and OUT movements are written
        in the same caller-owned transaction as the Step RUNNING transition.
        """
        step = self._session.scalar(
            select(JobStep).where(JobStep.job_step_id == job_step_id).with_for_update()
        )
        if step is None:
            raise InventoryReservationInvariantError(f"JobStep not found: {job_step_id}.")
        if step.inventory_consumed_at is not None:
            return False
        requirements = self._step_requirements(step=step)
        if not requirements:
            return False
        inventories = self._locked_inventory_rows(requirements)
        for part_code, quantity in requirements.items():
            inventory = inventories.get(part_code)
            if inventory is None or inventory.quantity < quantity or inventory.reserved_quantity < quantity:
                raise InventoryReservationInvariantError(
                    f"Reserved inventory is missing for job_step_id={step.job_step_id}, "
                    f"part_code={part_code}, quantity={quantity}."
                )
        for part_code, quantity in requirements.items():
            inventory = inventories[part_code]
            inventory.quantity -= quantity
            inventory.reserved_quantity -= quantity
            self._session.add(
                InventoryMovement(
                    part_code=part_code,
                    job_id=step.job_id,
                    job_step_id=step.job_step_id,
                    movement_type=MovementType.OUT,
                    quantity=quantity,
                    reason=f"Production execution inventory issue for JobStep {step.job_step_id}.",
                )
            )
        step.inventory_consumed_at = self._utcnow()
        self._session.flush()
        return True

    def release_unused_job_reservations(self, *, job: ProductionJob) -> bool:
        """Release only never-issued Job requirements at FAILED/CANCELED terminalization."""
        locked_job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job.job_id).with_for_update()
        )
        if locked_job is None:
            raise InventoryReservationInvariantError(f"ProductionJob not found: {job.job_id}.")
        if locked_job.inventory_reservation_released_at is not None:
            return False
        requirements = self._unused_job_requirements(job_id=locked_job.job_id)
        inventories = self._locked_inventory_rows(requirements)
        for part_code, quantity in requirements.items():
            inventory = inventories.get(part_code)
            if inventory is None or inventory.reserved_quantity < quantity:
                raise InventoryReservationInvariantError(
                    f"Cannot release unconsumed reservation for job_id={locked_job.job_id}, "
                    f"part_code={part_code}, quantity={quantity}."
                )
        for part_code, quantity in requirements.items():
            inventories[part_code].reserved_quantity -= quantity
        locked_job.inventory_reservation_released_at = self._utcnow()
        self._session.flush()
        return True

    def _job_requirements(self, *, job_id: int) -> dict[str, int]:
        items = list(self._session.scalars(
            select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job_id)
            .order_by(JobMaterialDeliveryItem.part_code, JobMaterialDeliveryItem.delivery_item_id)
            .with_for_update()
        ))
        return self._aggregate(items)

    def _unused_job_requirements(self, *, job_id: int) -> dict[str, int]:
        items = list(self._session.scalars(
            select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job_id)
            .order_by(JobMaterialDeliveryItem.part_code, JobMaterialDeliveryItem.delivery_item_id)
            .with_for_update()
        ))
        step_ids = sorted({item.job_step_id for item in items if item.job_step_id is not None})
        consumed_at: dict[int, datetime | None] = {}
        if step_ids:
            consumed_at = dict(self._session.execute(
                select(JobStep.job_step_id, JobStep.inventory_consumed_at)
                .where(JobStep.job_step_id.in_(step_ids))
                .with_for_update()
            ).all())
        # ``goal_accepted_at`` is durable evidence that the Robot Cell may
        # already have begun physically issuing this Step's material. It is
        # deliberately stronger than the attempt's current status: an
        # accepted attempt can later be marked UNKNOWN/FAILED during restart
        # recovery, but that must not turn its still-unrecorded issue back
        # into releasable available stock.
        accepted_step_ids: set[int] = set()
        if step_ids:
            accepted_step_ids = set(self._session.scalars(
                select(ExecutionAttempt.job_step_id)
                .where(
                    ExecutionAttempt.job_step_id.in_(step_ids),
                    ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
                    ExecutionAttempt.goal_accepted_at.is_not(None),
                )
                .with_for_update()
            ))

        return self._aggregate(
            item for item in items
            if item.job_step_id is None
            or (
                consumed_at.get(item.job_step_id) is None
                and item.job_step_id not in accepted_step_ids
            )
        )

    def _step_requirements(self, *, step: JobStep) -> dict[str, int]:
        items = list(self._session.scalars(
            select(JobMaterialDeliveryItem)
            .where(JobMaterialDeliveryItem.job_step_id == step.job_step_id)
            .order_by(JobMaterialDeliveryItem.part_code, JobMaterialDeliveryItem.delivery_item_id)
            .with_for_update()
        ))
        if step.part_code is not None and step.quantity is not None and not items:
            raise InventoryReservationInvariantError(
                f"Material JobStep {step.job_step_id} has no immutable JobMaterialDeliveryItem."
            )
        return self._aggregate(items)

    @staticmethod
    def _aggregate(items) -> dict[str, int]:
        requirements: dict[str, int] = defaultdict(int)
        for item in items:
            if not item.part_code or not isinstance(item.quantity, int) or item.quantity <= 0:
                raise InventoryReservationInvariantError("Job-local material requirement is invalid.")
            requirements[item.part_code] += item.quantity
        return dict(sorted(requirements.items()))

    def _locked_inventory_rows(self, requirements: dict[str, int]) -> dict[str, Inventory]:
        if not requirements:
            return {}
        part_codes = tuple(sorted(requirements))
        # Lock Part then Inventory in the same deterministic part-code order.
        parts = list(self._session.scalars(
            select(Part).where(Part.part_code.in_(part_codes)).order_by(Part.part_code).with_for_update()
        ))
        if [part.part_code for part in parts] != list(part_codes):
            raise InventoryReservationInvariantError("A reserved production part is missing.")
        inventories = list(self._session.scalars(
            select(Inventory).where(Inventory.part_code.in_(part_codes)).order_by(Inventory.part_code).with_for_update()
        ))
        return {inventory.part_code: inventory for inventory in inventories}

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
