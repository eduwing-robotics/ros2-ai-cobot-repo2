from __future__ import annotations
from dataclasses import dataclass
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select
from shared.models.factory import ProductionJob, JobStatus, JobStep, ProductionJobControlState, StepStatus
from shared.services.unity_current_stage_projection_service import UnityCurrentStageProjectionService

@dataclass
class ProductionStatusQueryResult:
    status: JobStatus
    product_name: str
    control_state: ProductionJobControlState | None = None
    current_step_name: str | None = None
    failure_reason: str | None = None
    process_stage_code: str | None = None

class ProductionStatusQueryService:
    def __init__(self, session: Session):
        self._session = session

    def get_active_job_status(self) -> ProductionStatusQueryResult | None:
        active_statuses = {
            JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
            JobStatus.PAUSED, JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY
        }

        job = self._session.scalar(
            select(ProductionJob)
            .where(ProductionJob.status.in_(active_statuses))
            .options(selectinload(ProductionJob.steps), selectinload(ProductionJob.product))
            .order_by(ProductionJob.job_id.desc())
            .limit(1)
        )

        if not job:
            job = self._session.scalar(
                select(ProductionJob)
                .options(selectinload(ProductionJob.steps), selectinload(ProductionJob.product))
                .order_by(ProductionJob.job_id.desc())
                .limit(1)
            )

        if not job:
            return None

        product_name = job.product.product_name if job.product else job.product_code

        if job.status == JobStatus.COMPLETED:
            process_stage = UnityCurrentStageProjectionService(self._session).derive_process_stage(job=job)
            return ProductionStatusQueryResult(
                status=job.status,
                product_name=product_name,
                control_state=job.control_state,
                current_step_name=(str(process_stage["process_stage_display_name"]) if process_stage else None),
                process_stage_code=(str(process_stage["process_stage_code"]) if process_stage else None),
            )
        elif job.status == JobStatus.FAILED:
            failed_step = next((s for s in job.steps if s.status == StepStatus.FAILED), None)
            return ProductionStatusQueryResult(
                status=job.status,
                product_name=product_name,
                control_state=job.control_state,
                current_step_name=failed_step.resolved_display_name if failed_step else None,
                failure_reason=failed_step.failure_reason if failed_step else None
            )
        elif job.status is JobStatus.CANCELED:
            return ProductionStatusQueryResult(
                status=job.status,
                product_name=product_name,
                control_state=job.control_state,
            )
        else: # RUNNING, READY, REQUESTED, PRE_ROOF_READY, ROOF_READY, PAUSED
            # Voice status is intentionally derived from the same durable
            # 12-stage projection as Unity.  Do not recreate a Voice-only
            # interpretation from the next JobStep: QA, transport, returns,
            # PRE_ROOF, and outbound have no equivalent runnable Step.
            process_stage = UnityCurrentStageProjectionService(self._session).derive_process_stage(job=job)
            if process_stage is not None:
                return ProductionStatusQueryResult(
                    status=job.status,
                    product_name=product_name,
                    control_state=job.control_state,
                    current_step_name=str(process_stage["process_stage_display_name"]),
                    process_stage_code=str(process_stage["process_stage_code"]),
                )
            return ProductionStatusQueryResult(
                status=job.status,
                product_name=product_name,
                control_state=job.control_state,
            )
