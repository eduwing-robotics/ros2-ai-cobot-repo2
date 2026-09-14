"""Read-only composition of one ProductionJob execution diagnostic snapshot."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from shared.models.factory import (
    EventType,
    JobStatus,
    JobStep,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionResultCode,
    ProductionInspectionType,
    ProductionInspectionViewRequest,
    ProductionJob,
    ProductionJobControlState,
    StepStatus,
)
from shared.schemas.production import (
    ExecutionEventSnapshotResponse,
    ExecutionStepSnapshotResponse,
    ProductionExecutionSnapshotResponse,
    ProductionInspectionResponse,
    ProductionRoofSnapshotResponse,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService


class ProductionExecutionSnapshotError(RuntimeError):
    """Base error for the read-only execution snapshot boundary."""


class ProductionExecutionSnapshotJobNotFoundError(ProductionExecutionSnapshotError):
    """Raised when the requested ProductionJob does not exist."""


class ProductionExecutionSnapshotInconsistencyError(ProductionExecutionSnapshotError):
    """Raised instead of hiding more than one concurrently running JobStep."""


class ProductionExecutionSnapshotService:
    """Compose existing read boundaries without any write, req_id, or dispatch.

    Step selection remains owned by ``ProductionOrchestrationService`` and material
    prerequisites remain owned by ``StepReadinessService``. The Robot Cell adapter
    is used only through its pure contract assessment; no transport or process-local
    idempotency registry is touched.
    """

    _STEP_EXECUTION_EVENTS = (
        EventType.STEP_STARTED,
        EventType.STEP_COMPLETED,
        EventType.STEP_FAILED,
    )

    def __init__(self, session: Session) -> None:
        self._session = session
        self._orchestration = ProductionOrchestrationService(session)
        self._readiness = StepReadinessService(MaterialDeliveryService(session))

    def get_snapshot(self, *, job_id: int) -> ProductionExecutionSnapshotResponse:
        """Return a diagnostic view of one Job without flushing or mutating state."""

        job = self._session.scalar(
            select(ProductionJob)
            .options(joinedload(ProductionJob.product))
            .where(ProductionJob.job_id == job_id)
        )
        if job is None:
            raise ProductionExecutionSnapshotJobNotFoundError(
                f"Production job not found: job_id={job_id}."
            )

        running_steps = list(
            self._session.scalars(
                select(JobStep)
                .where(JobStep.job_id == job.job_id, JobStep.status == StepStatus.RUNNING)
                .order_by(JobStep.step_order.asc(), JobStep.job_step_id.asc())
            )
        )
        if len(running_steps) > 1:
            step_ids = ", ".join(str(step.job_step_id) for step in running_steps)
            raise ProductionExecutionSnapshotInconsistencyError(
                f"Production job job_id={job.job_id} has multiple RUNNING JobSteps: {step_ids}."
            )

        current_step = running_steps[0] if running_steps else None
        next_step = self._get_next_pending_step(job=job, current_step=current_step)
        inspection = self._session.scalar(
            select(ProductionInspection)
            .where(
                ProductionInspection.production_job_id == job.job_id,
                ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
            )
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )
        roof_step = self._session.scalar(
            select(JobStep)
            .where(JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF")
        )

        return ProductionExecutionSnapshotResponse(
            job_id=job.job_id,
            job_status=job.status,
            control_state=job.control_state,
            product_code=job.product.product_code,
            current_step=self._to_step_snapshot(current_step) if current_step is not None else None,
            next_step=self._to_next_step_snapshot(job=job, step=next_step)
            if next_step is not None
            else None,
            inspection=self._to_inspection_snapshot(inspection) if inspection is not None else None,
            roof=ProductionRoofSnapshotResponse(
                roof_option_code=job.roof_option_code,
                step=self._to_step_snapshot(roof_step) if roof_step is not None else None,
            ),
            last_event=self._latest_event(job_id=job.job_id),
            last_step_execution_event=self._latest_step_execution_event(job_id=job.job_id),
        )

    def _get_next_pending_step(self, *, job: ProductionJob, current_step: JobStep | None) -> JobStep | None:
        """Reuse lifecycle selection while not presenting inactive Jobs as executable."""

        selected = self._orchestration.get_next_step(job.job_id)
        if current_step is not None or job.status not in {JobStatus.RUNNING, JobStatus.ROOF_READY}:
            return None
        if selected is None or selected.status is not StepStatus.PENDING:
            return None
        return selected

    def _to_next_step_snapshot(
        self, *, job: ProductionJob, step: JobStep
    ) -> ExecutionStepSnapshotResponse:
        readiness = self._readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)
        if not readiness.ready:
            return ExecutionStepSnapshotResponse(
                **self._step_data(step),
                ready=False,
                readiness_reason=readiness.reason.value if readiness.reason is not None else None,
                dispatchable=False,
                dispatch_block_reason="NOT_READY",
            )
        if job.control_state is not ProductionJobControlState.ACTIVE:
            return ExecutionStepSnapshotResponse(
                **self._step_data(step),
                ready=True,
                readiness_reason=None,
                dispatchable=False,
                dispatch_block_reason=f"JOB_CONTROL_{job.control_state.value}",
            )
        dispatch = RobotCellActionAdapter.assess_dispatchability(job=job, step=step)
        return ExecutionStepSnapshotResponse(
            **self._step_data(step),
            ready=True,
            readiness_reason=None,
            dispatchable=dispatch.dispatchable,
            dispatch_block_reason=dispatch.block_reason.value if dispatch.block_reason is not None else None,
        )

    def _latest_event(self, *, job_id: int) -> ExecutionEventSnapshotResponse | None:
        event = self._session.scalar(
            select(ProductionEvent)
            .where(ProductionEvent.job_id == job_id)
            .order_by(ProductionEvent.created_at.desc(), ProductionEvent.event_id.desc())
            .limit(1)
        )
        return self._to_event_snapshot(event) if event is not None else None

    def _latest_step_execution_event(self, *, job_id: int) -> ExecutionEventSnapshotResponse | None:
        event = self._session.scalar(
            select(ProductionEvent)
            .where(
                ProductionEvent.job_id == job_id,
                ProductionEvent.job_step_id.is_not(None),
                ProductionEvent.event_type.in_(self._STEP_EXECUTION_EVENTS),
            )
            .order_by(ProductionEvent.created_at.desc(), ProductionEvent.event_id.desc())
            .limit(1)
        )
        return self._to_event_snapshot(event) if event is not None else None

    @classmethod
    def _to_step_snapshot(cls, step: JobStep) -> ExecutionStepSnapshotResponse:
        return ExecutionStepSnapshotResponse(
            **cls._step_data(step),
            ready=None,
            readiness_reason=None,
            dispatchable=None,
            dispatch_block_reason=None,
        )

    @staticmethod
    def _step_data(step: JobStep) -> dict[str, object]:
        return {
            "job_step_id": step.job_step_id,
            "step_order": step.resolved_step_order,
            "step_code": step.resolved_step_code,
            "step_name": step.resolved_display_name,
            "operation_code": step.operation_code,
            "source_recipe_stage_id": step.source_recipe_stage_id,
            "supply_mode": step.supply_mode,
            "status": step.status,
            "started_at": step.started_at,
            "completed_at": step.completed_at,
            "failure_reason": step.failure_reason,
            "operator_execution_ready_at": step.operator_execution_ready_at,
        }

    def _to_inspection_snapshot(self, inspection: ProductionInspection) -> ProductionInspectionResponse:
        requests = list(self._session.scalars(select(ProductionInspectionViewRequest).where(
            ProductionInspectionViewRequest.inspection_id == inspection.inspection_id
        ).order_by(ProductionInspectionViewRequest.view_request_id)))
        latest = {row.view_name: row for row in requests}
        view_order = ("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")
        views = []
        for name in view_order:
            row = latest.get(name)
            if row is None:
                views.append({"view_name": name, "status": "PENDING", "result": None})
            elif row.status == "COMPLETED":
                views.append({"view_name": name, "status": row.result.value if row.result else "COMPLETED", "result": row.result})
            else:
                views.append({"view_name": name, "status": "IN_PROGRESS", "result": None})
        active = next((row for row in reversed(requests) if row.status in {"REQUESTED", "SENT", "ACKED"}), None)
        gate_state = "RELEASED" if (
            inspection.status.value == "COMPLETED" and inspection.result is ProductionInspectionResultCode.PASS
        ) else "NOT_RELEASED"
        return ProductionInspectionResponse(
            inspection_id=inspection.inspection_id,
            inspection_type=inspection.inspection_type,
            inspection_cycle=inspection.inspection_cycle,
            inspection_request_id=inspection.inspection_request_id,
            status=inspection.status,
            result=inspection.result,
            vision_production_valid=inspection.vision_production_valid,
            production_valid=inspection.production_valid,
            requested_at=inspection.requested_at,
            started_at=inspection.started_at,
            completed_at=inspection.completed_at,
            failure_reason=inspection.failure_reason,
            current_view=active.view_name if active is not None else None,
            views=views if requests else [],
            gate_state=gate_state,
        )

    @staticmethod
    def _to_event_snapshot(event: ProductionEvent) -> ExecutionEventSnapshotResponse:
        return ExecutionEventSnapshotResponse(
            event_id=event.event_id,
            event_type=event.event_type,
            job_step_id=event.job_step_id,
            error_code=event.error_code,
            message=event.message,
            created_at=event.created_at,
        )
