"""Read-only Unity timeline stage projection for the current HOUSE_B lifecycle."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    ProductionInspection,
    ProductionInspectionType,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    DropResourceService,
)
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService


class UnityCurrentStageProjectionService:
    """Map durable lifecycle evidence to one Unity display-stage code.

    This is intentionally a display projection. It creates no state, makes no
    dispatch decision, and never replaces detailed JobStep or transport status.
    """

    JOB_REQUESTED = "JOB_REQUESTED"
    INCOMING_INSPECTION = "INCOMING_INSPECTION"
    INSTALL_BASE = "INSTALL_BASE"
    DELIVER_INNER_WALL = "DELIVER_INNER_WALL"
    INSTALL_INNER_WALL = "INSTALL_INNER_WALL"
    RETURN_INNER_PALLET = "RETURN_INNER_PALLET"
    DELIVER_OUTER_WALL = "DELIVER_OUTER_WALL"
    INSTALL_OUTER_WALL = "INSTALL_OUTER_WALL"
    RETURN_OUTER_PALLET = "RETURN_OUTER_PALLET"
    PRE_ROOF_INSPECTION = "PRE_ROOF_INSPECTION"
    INSTALL_ROOF = "INSTALL_ROOF"

    _ACTIVE_ATTEMPT_STATUSES = (
        ExecutionAttemptStatus.CREATED,
        ExecutionAttemptStatus.DISPATCHING,
        ExecutionAttemptStatus.ACCEPTED,
        ExecutionAttemptStatus.UNKNOWN,
    )
    _OUTER_OPERATIONS = frozenset({
        "INSTALL_LEFT_OUTER_WALL",
        "INSTALL_RIGHT_OUTER_WALL",
        "INSTALL_REAR_OUTER_WALL",
        "INSTALL_DOOR_OUTER_WALL",
    })

    _PROCESS_STAGES = {
        "COMMAND_RECEIVED": (1, "작업 명령 전달"), "INCOMING_QA": (2, "수입검사"),
        "BASE_INSTALL": (3, "Zekeep 베이스 설치"), "OUTER_WALL_DELIVERY": (4, "외벽 팔레트 운반"),
        "OUTER_WALL_INSTALL": (5, "FR5 외벽 설치"),
        "OUTER_RETURN_INNER_DELIVERY": (6, "외벽 빈 팔레트 회수 / 내벽 팔레트 운반"),
        "INNER_WALL_INSTALL": (7, "FR5 내벽 설치"), "INNER_WALL_RETURN": (8, "내벽 빈 팔레트 회수"),
        "PRE_ROOF_INSPECTION": (9, "조립 결과 검사"), "ROOF_INSTALL": (10, "Zekeep 지붕 설치"),
        "HOUSE_OUTBOUND": (11, "FR5 완성 주택 운반"), "COMPLETED": (12, "작업 완료"),
    }

    def __init__(self, session: Session) -> None:
        self._session = session
        self._qa = IncomingQAOrchestrationService(session)

    def derive(self, *, job: ProductionJob) -> str | None:
        """Derive the representative timeline stage for one loaded Job."""
        if job.status is JobStatus.COMPLETED:
            return self.INSTALL_ROOF if self._has_completed_roof(job_id=job.job_id) else None
        if job.status in {JobStatus.FAILED, JobStatus.CANCELED}:
            return None

        readiness = self._qa.preproduction_readiness(job_id=job.job_id)
        if not readiness.ready:
            if job.status is JobStatus.REQUESTED and not self._has_any_incoming_inspection(job_id=job.job_id):
                return self.JOB_REQUESTED
            return self.INCOMING_INSPECTION

        # PRE_ROOF is the primary production milestone. Pallet return and HOME
        # may continue concurrently, but remain represented by transport_status.
        if job.status is JobStatus.PRE_ROOF_READY or self._pre_roof_is_holding(job_id=job.job_id):
            return self.PRE_ROOF_INSPECTION
        if job.status is JobStatus.ROOF_READY:
            return self.INSTALL_ROOF

        steps = list(self._session.scalars(
            select(JobStep)
            .where(JobStep.job_id == job.job_id)
            .order_by(JobStep.step_order, JobStep.job_step_id)
        ))
        roof = next((step for step in steps if step.operation_code == "INSTALL_ROOF"), None)
        if roof is not None and roof.status is not StepStatus.COMPLETED:
            return self.INSTALL_ROOF

        current = next((step for step in steps if step.status is not StepStatus.COMPLETED), None)
        if current is None:
            return None
        if current.operation_code == "INSTALL_BASE":
            return self.INSTALL_BASE
        if current.operation_code == "INSTALL_INNER_WALL":
            return self._inner_stage(job_id=job.job_id, step=current)
        if current.operation_code in self._OUTER_OPERATIONS:
            return self._outer_stage(job_id=job.job_id, steps=steps)
        return None

    def derive_process_stage(self, *, job: ProductionJob) -> dict[str, object] | None:
        """Return the one authoritative additive 12-stage PROCESS projection."""
        code = self._derive_process_stage_code(job=job)
        if code is None:
            return None
        order, display_name = self._PROCESS_STAGES[code]
        return {"process_stage_code": code, "process_stage_order": order,
                "process_stage_display_name": display_name}

    def _derive_process_stage_code(self, *, job: ProductionJob) -> str | None:
        if job.status is JobStatus.COMPLETED:
            return "COMPLETED"
        if job.status in {JobStatus.FAILED, JobStatus.CANCELED}:
            return None
        readiness = self._qa.preproduction_readiness(job_id=job.job_id)
        if not readiness.ready:
            return ("COMMAND_RECEIVED" if job.status is JobStatus.REQUESTED
                    and not self._has_any_incoming_inspection(job_id=job.job_id) else "INCOMING_QA")
        steps = list(self._session.scalars(select(JobStep).where(JobStep.job_id == job.job_id)
                                           .order_by(JobStep.step_order, JobStep.job_step_id)))
        base = next((s for s in steps if s.operation_code == "INSTALL_BASE"), None)
        outer_steps = [s for s in steps if s.operation_code in self._OUTER_OPERATIONS]
        inner_steps = [s for s in steps if s.operation_code == "INSTALL_INNER_WALL"]
        roof = next((s for s in steps if s.operation_code == "INSTALL_ROOF"), None)
        outer = self._transported_delivery(job_id=job.job_id, group_code="OUTER_WALLS")
        inner = self._transported_delivery(job_id=job.job_id, group_code="INNER_WALL")
        if base is not None and base.status is not StepStatus.COMPLETED:
            return "BASE_INSTALL"
        if outer is not None and not self._forward_delivery_arrived(delivery=outer):
            return "OUTER_WALL_DELIVERY"
        if outer_steps and any(s.status is not StepStatus.COMPLETED for s in outer_steps):
            return "OUTER_WALL_INSTALL"
        if inner_steps and any(s.status is not StepStatus.COMPLETED for s in inner_steps):
            if inner is not None and not self._inner_delivery_ready_after_outer_return(outer=outer, inner=inner):
                return "OUTER_RETURN_INNER_DELIVERY"
            return "INNER_WALL_INSTALL"
        if inner is not None and not self._return_succeeded(delivery=inner):
            return "INNER_WALL_RETURN"
        if job.status is JobStatus.PRE_ROOF_READY or self._pre_roof_is_holding(job_id=job.job_id):
            return "PRE_ROOF_INSPECTION"
        if roof is None or roof.status is not StepStatus.COMPLETED:
            return "ROOF_INSTALL"
        return "HOUSE_OUTBOUND"

    def _forward_delivery_arrived(self, *, delivery: JobMaterialDelivery) -> bool:
        return (delivery.status is MaterialDeliveryStatus.COMPLETED
                and self._has_succeeded_attempt(delivery=delivery, command_type=MATERIAL_TRANSPORT_COMMAND_TYPE))

    def _inner_delivery_ready_after_outer_return(self, *, outer: JobMaterialDelivery | None,
                                                 inner: JobMaterialDelivery) -> bool:
        return ((outer is None or self._return_succeeded(delivery=outer))
                and self._forward_delivery_arrived(delivery=inner)
                and DropResourceService(self._session).get_drop_state().owner_delivery_id == inner.job_delivery_id)

    def _return_succeeded(self, *, delivery: JobMaterialDelivery) -> bool:
        return self._has_succeeded_attempt(delivery=delivery, command_type=EMPTY_RETURN_COMMAND_TYPE)

    def _has_succeeded_attempt(self, *, delivery: JobMaterialDelivery, command_type: str) -> bool:
        return self._session.scalar(select(ExecutionAttempt.attempt_id).where(
            ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
            ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
            ExecutionAttempt.command_type == command_type,
            ExecutionAttempt.status == ExecutionAttemptStatus.SUCCEEDED,
        ).limit(1)) is not None
    def _inner_stage(self, *, job_id: int, step: JobStep) -> str:
        outer = self._transported_delivery(job_id=job_id, group_code="OUTER_WALLS")
        if outer is not None and self._return_is_pending(delivery=outer):
            return self.RETURN_OUTER_PALLET
        delivery = self._transported_delivery(job_id=job_id, group_code="INNER_WALL")
        if delivery is None or delivery.status is not MaterialDeliveryStatus.COMPLETED:
            return self.DELIVER_INNER_WALL
        return self.INSTALL_INNER_WALL

    def _outer_stage(self, *, job_id: int, steps: list[JobStep]) -> str:
        delivery = self._transported_delivery(job_id=job_id, group_code="OUTER_WALLS")
        if delivery is None or delivery.status is not MaterialDeliveryStatus.COMPLETED:
            return self.DELIVER_OUTER_WALL
        if any(
            step.operation_code in self._OUTER_OPERATIONS
            and step.status is not StepStatus.COMPLETED
            for step in steps
        ):
            return self.INSTALL_OUTER_WALL
        # Normally Job status becomes PRE_ROOF_READY immediately and wins above.
        # This preserves an honest display for any durable intermediate state.
        return self.RETURN_OUTER_PALLET

    def _transported_delivery(self, *, job_id: int, group_code: str) -> JobMaterialDelivery | None:
        return self._session.scalar(
            select(JobMaterialDelivery)
            .where(
                JobMaterialDelivery.production_job_id == job_id,
                JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
                JobMaterialDelivery.supply_group_code == group_code,
            )
            .order_by(JobMaterialDelivery.job_delivery_id)
        )

    def _return_is_pending(self, *, delivery: JobMaterialDelivery) -> bool:
        attempts = list(self._session.scalars(
            select(ExecutionAttempt).where(
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
            )
        ))
        if any(attempt.status is ExecutionAttemptStatus.SUCCEEDED for attempt in attempts):
            return False
        if any(attempt.status in self._ACTIVE_ATTEMPT_STATUSES for attempt in attempts):
            return True
        return (
            delivery.status is MaterialDeliveryStatus.COMPLETED
            and self._all_consuming_steps_completed(delivery_id=delivery.job_delivery_id)
        )

    def _all_consuming_steps_completed(self, *, delivery_id: int) -> bool:
        items = list(self._session.scalars(
            select(JobMaterialDeliveryItem).where(
                JobMaterialDeliveryItem.job_delivery_id == delivery_id
            )
        ))
        if not items or any(item.job_step_id is None for item in items):
            return False
        steps = [self._session.get(JobStep, item.job_step_id) for item in items]
        return all(step is not None and step.status is StepStatus.COMPLETED for step in steps)

    def _has_any_incoming_inspection(self, *, job_id: int) -> bool:
        return self._session.scalar(
            select(MaterialInspection.inspection_id)
            .join(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job_id)
            .limit(1)
        ) is not None

    def _pre_roof_is_holding(self, *, job_id: int) -> bool:
        inspection = self._session.scalar(
            select(ProductionInspection)
            .where(
                ProductionInspection.production_job_id == job_id,
                ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
            )
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )
        return inspection is not None and not (
            inspection.status.value == "COMPLETED"
            and inspection.result is not None
            and inspection.result.value == "PASS"
        )

    def _has_completed_roof(self, *, job_id: int) -> bool:
        return self._session.scalar(
            select(JobStep.job_step_id).where(
                JobStep.job_id == job_id,
                JobStep.operation_code == "INSTALL_ROOF",
                JobStep.status == StepStatus.COMPLETED,
            )
        ) is not None
