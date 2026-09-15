"""Read-only boundary for materializing authoritative ExecuteTask parts payloads.

It resolves normalized Stage→Part, explicit installation-target slot, pick-zone, and roof-option
master data. Missing facts remain explicit diagnostics; no synthetic payload is
created and this module never contacts ROS or mutates Production state.
"""

from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.services.production_part_mapping_service import (
    CellPayloadSourceReason,
    ProductionCellPayloadContextError,
    ProductionCellPayloadSourceMissingError,
    ProductionPartMappingService,
)

from fms_server.robot_cell_slot_mapper import RobotCellSlotMapper

from fms_server.robot_cell_action_adapter import (
    CellPartSpec,
    RobotCellPartClassMapper,
)
from shared.models.factory import JobStep, ProductionJob



@dataclass(frozen=True, slots=True)
class CellPartsPayload:
    """Structured payload resolved from Production sources before adapter serialization."""

    parts: tuple[CellPartSpec, ...]

class ProductionCellPayloadBuilder:
    """Build structured Cell parts from explicit installation-target allocations.

    Operation→class remains a pure contract mapper. The mapping service owns
    allocation selection and quantity integrity; this builder only converts each
    resolved physical item to ``CellPartSpec``.
    """
    def __init__(self, session: Session) -> None:
        self._session = session
        self._mapping = ProductionPartMappingService(session)

    def build_for_job_step(self, *, job_id: int, job_step_id: int) -> CellPartsPayload:
        _job, step = self._job_step_context(job_id=job_id, job_step_id=job_step_id)
        requirements = self._mapping.resolve_parts_for_job_step(
            job_id=job_id, job_step_id=job_step_id
        )
        return CellPartsPayload(parts=tuple(
            CellPartSpec(
                slot=RobotCellSlotMapper.map(
                    product_code=_job.product_code,
                    canonical_slot_code=requirement.slot,
                    part_code=requirement.part_code,
                ),
                # Vision's detailed classifier name and the Robot Cell's coarse
                # manipulation class are intentionally separate contracts.
                class_name=RobotCellPartClassMapper.map_operation_code(step.operation_code),
                part_code=requirement.part_code,
                zone=requirement.zone,
            )
            for requirement in requirements
        ))

    def build_for_material_feed(self, *, feed_execution_id: int) -> CellPartsPayload:
        self._mapping.validate_material_feed_mapping(feed_execution_id=feed_execution_id)
        raise AssertionError("validate_material_feed_mapping must fail until a Feed item mapping exists.")

    def _job_step_context(self, *, job_id: int, job_step_id: int) -> tuple[ProductionJob, JobStep]:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id)
        )
        step = self._session.scalar(
            select(JobStep).where(JobStep.job_step_id == job_step_id)
        )
        if job is None or step is None or step.job_id != job.job_id:
            raise ProductionCellPayloadContextError(
                "ProductionJob and JobStep must exist and belong to the same Job."
            )
        return job, step
