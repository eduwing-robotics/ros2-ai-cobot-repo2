"""Durable, generic human release between transported delivery and Cell execution."""

from __future__ import annotations
import logging

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from shared.realtime.production_events import get_production_change_callback

from fms_server.robot_cell_action_adapter import (
    MissingOperationCodeError,
    RobotCellTaskTypeMapper,
    UnknownOperationCodeError,
)
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialDeliveryStatus,
    JobStatus,
    ProductionJob,
    StepStatus,
    SupplyMode,
)


class OperatorExecutionReadyError(RuntimeError):
    """Base error for the explicit Robot Cell execution-release command."""


class OperatorExecutionReadyJobNotFoundError(OperatorExecutionReadyError):
    pass


class OperatorExecutionReadyStepNotFoundError(OperatorExecutionReadyError):
    pass


class OperatorExecutionReadyStateError(OperatorExecutionReadyError):
    pass


class OperatorExecutionReadyPolicyError(OperatorExecutionReadyError):
    pass


class OperatorExecutionReadyDeliveryIncompleteError(OperatorExecutionReadyError):
    pass


def is_robot_cell_execution_step(step: JobStep) -> bool:
    """Use the existing approved Cell task mapping as the generic capability signal."""

    try:
        task_type = RobotCellTaskTypeMapper.map_operation_code(step.operation_code)
    except (MissingOperationCodeError, UnknownOperationCodeError):
        return False
    # MATERIAL_FEED is a Robot Cell capability but not an assembly execution
    # step; its established feed lifecycle remains separately authoritative.
    return task_type != "MATERIAL_FEED"


def operator_execution_ready_required(step: JobStep) -> bool:
    """Whether this *pending* transported Cell step still needs operator release."""

    return (
        step.status is StepStatus.PENDING
        and step.supply_mode is SupplyMode.TRANSPORTED
        and step.operator_execution_ready_at is None
        and is_robot_cell_execution_step(step)
    )


class OperatorExecutionReadyService:
    """Persist a human safety release without dispatching Robot Cell work."""

    def __init__(self, session: Session, *, post_commit_callback=None) -> None:
        self._session = session
        self._post_commit_callback = post_commit_callback or get_production_change_callback()

    def confirm(
        self, *, job_id: int, job_step_id: int, commit: bool = True
    ) -> JobStep:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
        )
        if job is None:
            raise OperatorExecutionReadyJobNotFoundError(
                f"Production job not found: job_id={job_id}."
            )
        step = self._session.scalar(
            select(JobStep)
            .where(JobStep.job_step_id == job_step_id)
            .with_for_update()
        )
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            raise OperatorExecutionReadyStateError(
                f"Operator execution release is not allowed for terminal production job status {job.status.value}."
            )
        if step is None or step.job_id != job_id:
            raise OperatorExecutionReadyStepNotFoundError(
                "JobStep does not belong to the requested production job."
            )
        if step.status is not StepStatus.PENDING:
            raise OperatorExecutionReadyStateError(
                "Operator execution release is allowed only for a PENDING JobStep."
            )
        if step.supply_mode is not SupplyMode.TRANSPORTED or not is_robot_cell_execution_step(step):
            raise OperatorExecutionReadyPolicyError(
                "JobStep is not a transported Robot Cell execution step."
            )

        # Duplicate clicks preserve the original physical-clear confirmation.
        if step.operator_execution_ready_at is not None:
            return step

        deliveries = list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .join(JobMaterialDeliveryItem)
                .where(JobMaterialDeliveryItem.job_step_id == step.job_step_id)
                .with_for_update()
            )
        )
        if not deliveries or any(
            delivery.supply_mode is not step.supply_mode
            or delivery.supply_group_code != step.supply_group_code
            or delivery.supply_destination_code != step.supply_destination_code
            for delivery in deliveries
        ):
            raise OperatorExecutionReadyPolicyError(
                "JobStep has no valid transported delivery policy to release."
            )
        if any(delivery.status is not MaterialDeliveryStatus.COMPLETED for delivery in deliveries):
            raise OperatorExecutionReadyDeliveryIncompleteError(
                "All required transported deliveries must be COMPLETED before execution release."
            )

        step.operator_execution_ready_at = datetime.now(timezone.utc)
        if commit:
            self._session.commit()
            self._notify_production_changed(job_id, "operator_execution_ready")
        return step

    def confirm_outer_walls_batch(self, *, job_id: int) -> tuple[JobMaterialDelivery, list[JobStep]]:
        """Atomically record existing operator-clear facts for one OUTER_WALLS delivery."""
        try:
            job = self._session.scalar(
                select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
            )
            if job is None:
                raise OperatorExecutionReadyJobNotFoundError(
                    f"Production job not found: job_id={job_id}."
                )
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise OperatorExecutionReadyStateError(
                    f"Operator execution release is not allowed for terminal production job status {job.status.value}."
                )
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(
                    JobMaterialDelivery.production_job_id == job_id,
                    JobMaterialDelivery.supply_group_code == "OUTER_WALLS",
                    JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
                )
                .with_for_update()
            )
            if delivery is None:
                raise OperatorExecutionReadyPolicyError(
                    "No transported OUTER_WALLS delivery exists for this production job."
                )
            steps = list(self._session.scalars(
                select(JobStep)
                .join(JobMaterialDeliveryItem, JobMaterialDeliveryItem.job_step_id == JobStep.job_step_id)
                .where(JobMaterialDeliveryItem.job_delivery_id == delivery.job_delivery_id)
                .order_by(JobStep.step_order, JobStep.job_step_id)
                .with_for_update()
            ))
            if not steps:
                raise OperatorExecutionReadyPolicyError("OUTER_WALLS delivery has no JobSteps.")
            changed = False
            for step in steps:
                if step.status is StepStatus.COMPLETED:
                    continue
                was_ready = step.operator_execution_ready_at is not None
                self.confirm(job_id=job_id, job_step_id=step.job_step_id, commit=False)
                changed = changed or not was_ready
            self._session.commit()
            if changed:
                self._notify_production_changed(job_id, "outer_walls_operator_execution_ready")
            return delivery, steps
        except Exception:
            self._session.rollback()
            raise

    def _notify_production_changed(self, job_id: int, reason: str) -> None:
        callback = self._post_commit_callback
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:
            logging.getLogger(__name__).exception("Production post-commit notification failed job_id=%s reason=%s", job_id, reason)
