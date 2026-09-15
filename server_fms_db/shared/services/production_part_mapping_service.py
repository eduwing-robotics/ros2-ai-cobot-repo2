"""Authoritative Production Part and installation-target resolution.

``part_code`` identifies what is picked. ``pick_zone`` identifies where it is
searched for, while ``slot`` is the stable canonical Product installation target.
The Cell wire slot is derived later at the ExecuteTask payload boundary. The resolver never derives a slot from a
Part-owned storage location or chooses an allocation by list order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStep,
    ProductionJob,
)


class CellPayloadSourceReason(StrEnum):
    PART_MAPPING_MISSING = "PART_MAPPING_MISSING"
    SLOT_MAPPING_MISSING = "SLOT_MAPPING_MISSING"
    FEED_ITEM_MAPPING_MISSING = "FEED_ITEM_MAPPING_MISSING"
    MAPPING_INTEGRITY_INVALID = "MAPPING_INTEGRITY_INVALID"


class ProductionCellPayloadError(RuntimeError):
    """Base error for unavailable or invalid Production payload source data."""


class ProductionCellPayloadSourceMissingError(ProductionCellPayloadError):
    """Required authoritative data is absent; callers must not use a placeholder."""

    def __init__(self, reason: CellPayloadSourceReason, detail: str) -> None:
        self.reason = reason
        super().__init__(detail)


class ProductionCellPayloadContextError(ProductionCellPayloadError):
    """The requested runtime entity does not form a valid mapping context."""


@dataclass(frozen=True, slots=True)
class ResolvedProductionPart:
    """One explicitly allocated Cell payload item."""

    part_code: str
    vision_class: str
    slot: str
    zone: str | None


class ProductionPartMappingService:
    """Resolve normalized Part, installation-slot, and pick-zone mappings only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve_parts_for_job_step(
        self, *, job_id: int, job_step_id: int
    ) -> tuple[ResolvedProductionPart, ...]:
        job, step = self._job_step_context(job_id=job_id, job_step_id=job_step_id)

        if not step.part_code:
            raise ProductionCellPayloadSourceMissingError(
                CellPayloadSourceReason.PART_MAPPING_MISSING,
                "JobStep execution snapshot is missing part_code."
            )

        if not step.vision_class:
            raise ProductionCellPayloadSourceMissingError(
                CellPayloadSourceReason.PART_MAPPING_MISSING,
                "JobStep execution snapshot is missing vision_class."
            )

        if not step.slot_code:
            raise ProductionCellPayloadSourceMissingError(
                CellPayloadSourceReason.SLOT_MAPPING_MISSING,
                "JobStep execution snapshot is missing slot_code."
            )

        resolved: list[ResolvedProductionPart] = []
        quantity = step.quantity or 1
        for _ in range(quantity):
            resolved.append(
                ResolvedProductionPart(
                    part_code=step.part_code,
                    vision_class=step.vision_class,
                    slot=step.slot_code,
                    zone=step.pick_zone,
                )
            )

        return tuple(resolved)

    def validate_material_feed_mapping(self, *, feed_execution_id: int) -> None:
        """Keep Feed fail-closed until its own logistics mapping exists."""
        feed = self._session.scalar(
            select(JobMaterialFeedExecution).where(
                JobMaterialFeedExecution.feed_execution_id == feed_execution_id
            )
        )
        if feed is None:
            raise ProductionCellPayloadContextError(
                f"Material Feed execution not found: feed_execution_id={feed_execution_id}."
            )
        has_items = self._session.scalar(
            select(JobMaterialDeliveryItem.delivery_item_id)
            .where(JobMaterialDeliveryItem.job_delivery_id == feed.job_delivery_id)
            .limit(1)
        )
        if has_items is None:
            raise ProductionCellPayloadSourceMissingError(
                CellPayloadSourceReason.FEED_ITEM_MAPPING_MISSING,
                "MATERIAL_FEED has no delivery-item Part source for this batch.",
            )
        raise ProductionCellPayloadSourceMissingError(
            CellPayloadSourceReason.FEED_ITEM_MAPPING_MISSING,
            "MATERIAL_FEED delivery items do not authoritatively identify a Cell class, "
            "source/destination semantics, or immutable per-item logistics mapping.",
        )

    def _job_step_context(self, *, job_id: int, job_step_id: int) -> tuple[ProductionJob, JobStep]:
        job = self._session.get(ProductionJob, job_id)
        step = self._session.get(JobStep, job_step_id)
        if job is None or step is None or step.job_id != job.job_id:
            raise ProductionCellPayloadContextError(
                "ProductionJob and JobStep must exist and belong to the same Job."
            )
        return job, step
