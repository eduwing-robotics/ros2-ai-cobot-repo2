"""Transactional production Job/Step state management for the FMS.

This module is deliberately independent from FastAPI, ROS 2, Robot Cell, and transport
code.  It uses the existing ProcessStep reference data to initialize every production
job, then records state transitions and production events atomically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from shared.services.assembly_recipe_service import AssemblyRecipeService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.inventory_reservation_service import InventoryReservationService
from shared.services.production_configuration_validator import ProductionConfigurationValidator
from shared.services.execution_gate_service import (
    UnsupportedExecutionGateError,
    initial_materializable_stages,
)
from shared.services.drop_resource_service import EMPTY_RETURN_COMMAND_TYPE, DropResourceService
from shared.realtime.production_events import (
    ProductionChangeCallback,
    get_production_change_callback,
)

from shared.models.factory import (
    AssemblyRecipeStage,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    MaterialDeliveryStatus,
    SupplyMode,
    EventType,
    JobStatus,
    JobStep,
    Product,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    ProductionInspectionType,
    ProductionJob,
    RoofOptionCode,
    StepStatus,
)


class ProductionOrchestrationError(RuntimeError):
    """Base error for framework-independent production orchestration operations."""


class InvalidProductionInputError(ProductionOrchestrationError):
    """Raised when a Job identifier, code, or failure reason is invalid."""


class ProductNotFoundError(ProductionOrchestrationError):
    """Raised when a requested product does not exist."""


class ProductionJobNotFoundError(ProductionOrchestrationError):
    """Raised when a requested production Job does not exist."""


class JobStepNotFoundError(ProductionOrchestrationError):
    """Raised when a requested Job Step does not exist."""


class DuplicateJobCodeError(ProductionOrchestrationError):
    """Raised before the database unique constraint would reject a Job code."""


class InvalidProductionStateTransitionError(ProductionOrchestrationError):
    """Raised when a Job or Job Step is asked to make an unsupported transition."""

    def __init__(self, *, entity: str, current: str, target: str) -> None:
        super().__init__(f"Invalid {entity} state transition: {current} -> {target}.")


_Result = TypeVar("_Result")


TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX = "[TEST_OVERRIDE] synthetic RUNNING"


class ProductionOrchestrationService:
    """Manage production Job lifecycle using the existing Job/Step/Event schema.

    The caller supplies a SQLAlchemy Session. Every public write operation owns one
    transaction: it commits on success and rolls back both state and event records on
    any exception. PostgreSQL row locks protect concurrent FMS transition attempts;
    SQLite test databases simply ignore ``FOR UPDATE``.
    """

    def __init__(
        self, session: Session, *, post_commit_callback: ProductionChangeCallback | None = None
    ) -> None:
        self._session = session
        self._post_commit_callback = post_commit_callback or get_production_change_callback()

    def set_post_commit_callback(self, callback: ProductionChangeCallback | None) -> None:
        """Attach an FMS-owned best-effort notifier without changing DB semantics."""
        self._post_commit_callback = callback


    def create_job(
        self,
        *,
        product_code: str,
        job_code: str,
        roof_option_code: RoofOptionCode | None = None,
    ) -> ProductionJob:
        """Create one standalone REQUESTED Job and commit its complete lifecycle seed."""

        return self._run_write_transaction(
            lambda: self._create_job_in_transaction(
                product_code=product_code,
                job_code=job_code,
                roof_option_code=roof_option_code,
            ),
            reason="job_created",
        )

    def _create_job_in_transaction(
        self,
        *,
        product_code: str,
        job_code: str,
        roof_option_code: RoofOptionCode | None = None,
        source_pending_request_id: int | None = None,
        source_item_index: int | None = None,
        assembly_recipe_id: int | None = None,
    ) -> ProductionJob:
        """Build one Job/Step/Event set without committing.

        This internal primitive is shared by standalone Job creation and Phase 5
        materialization so recipe-backed JobSteps and JOB_CREATED have one source of truth.
        The caller owns the surrounding transaction.
        """

        normalized_job_code = self._normalize_job_code(job_code)
        normalized_roof_option_code = self._normalize_roof_option_code(roof_option_code)
        self._validate_pending_source(
            source_pending_request_id=source_pending_request_id,
            source_item_index=source_item_index,
        )

        normalized_product_code = product_code.strip() if isinstance(product_code, str) else ""
        if not normalized_product_code:
            raise InvalidProductionInputError("product_code must be a non-empty string.")

        product = self._session.scalar(select(Product).where(Product.product_code == normalized_product_code))
        if product is None:
            raise ProductNotFoundError(f"Product not found: product_code={normalized_product_code!r}.")
        if self._session.scalar(
            select(ProductionJob.job_id).where(ProductionJob.job_code == normalized_job_code)
        ) is not None:
            raise DuplicateJobCodeError(f"Production job code already exists: {normalized_job_code!r}.")

        recipe_service = AssemblyRecipeService(self._session)
        recipe = (
            recipe_service.get_recipe_for_product(
                recipe_id=assembly_recipe_id, product_code=product.product_code
            )
            if assembly_recipe_id is not None
            else recipe_service.get_active_recipe_for_product(product.product_code)
        )
        stages = AssemblyRecipeService.get_ordered_stages(recipe)
        selected_stages = [
            stage for stage in stages
            if stage.option_code is None
            or (normalized_roof_option_code is not None and stage.option_code == normalized_roof_option_code.value)
        ]
        initial_stages = self._initial_materializable_stages(selected_stages)
        # Validate the work that this Job can materialize now. Gated stages are
        # validated again at their explicit runtime gate, never silently skipped.
        ProductionConfigurationValidator.validate(recipe=recipe, stages=selected_stages)
        ProductionConfigurationValidator.validate_terminal_lifecycle(recipe=recipe, stages=selected_stages)
        job = ProductionJob(
            job_code=normalized_job_code,
            product_code=product.product_code,
            assembly_recipe_id=recipe.recipe_id,
            roof_option_code=normalized_roof_option_code,
            source_pending_request_id=source_pending_request_id,
            source_item_index=source_item_index,
            status=JobStatus.REQUESTED,
        )
        self._session.add(job)
        self._session.flush()
        self._session.add_all(
            self.snapshot_recipe_stage_to_job_step(job_id=job.job_id, stage=stage)
            for stage in initial_stages
        )
        self._session.flush()
        material_delivery_service = MaterialDeliveryService(self._session)
        material_delivery_service.instantiate_for_job(job=job)
        material_delivery_service.pre_materialize_gated_stage_materials(
            job=job, stages=stages
        )
        # Both initial and PRE_ROOF-gated DeliveryItems are now durable Job-local
        # snapshots. Reserve them in this same create-job transaction; gated
        # JobSteps remain deliberately unmaterialized.
        InventoryReservationService(self._session).reserve_job_requirements(job=job)
        self._record_event(
            job_id=job.job_id,
            job_step_id=None,
            event_type=EventType.JOB_CREATED,
            message=f"Production job {job.job_code} created.",
        )
        self._session.flush()
        return job

    @staticmethod
    def _initial_materializable_stages(
        stages: list[AssemblyRecipeStage],
    ) -> list[AssemblyRecipeStage]:
        return initial_materializable_stages(stages)

    @staticmethod
    def snapshot_recipe_stage_to_job_step(
        *, job_id: int, stage: AssemblyRecipeStage
    ) -> JobStep:
        """Create one immutable JobStep snapshot from authoritative RecipeStage data."""
        return JobStep(
            job_id=job_id,
            source_recipe_stage_id=stage.recipe_stage_id,
            step_order=stage.stage_order,
            operation_code=stage.operation_code,
            display_name=stage.display_name,
            part_code=stage.part_code,
            quantity=stage.quantity,
            slot_code=stage.slot_code,
            pick_zone=stage.pick_zone,
            roof_option_code=stage.option_code,
            vision_class=stage.part.vision_class if stage.part else None,
            supply_mode=stage.supply_mode,
            supply_group_code=stage.supply_group_code,
            supply_destination_code=stage.supply_destination_code,
            is_terminal=stage.is_terminal,
            status=StepStatus.PENDING,
        )

    def get_next_step(self, job_id: int) -> JobStep | None:
        """Return the active Step, otherwise the earliest PENDING Step by DB order.

        Returning a RUNNING Step first prevents an FMS caller from treating a later
        PENDING Step as dispatchable while a previous Step is still in progress.
        """

        self._validate_positive_id(job_id, field_name="job_id")
        if self._session.get(ProductionJob, job_id) is None:
            raise ProductionJobNotFoundError(f"Production job not found: job_id={job_id}.")

        for status in (StepStatus.RUNNING, StepStatus.PENDING):
            steps = list(
                self._session.scalars(
                    select(JobStep)
                    .where(JobStep.job_id == job_id, JobStep.status == status)
                )
            )
            if steps:
                return min(steps, key=lambda step: step.resolved_step_order)
        return None

    def start_job(self, job_id: int) -> ProductionJob:
        """Transition a REQUESTED Job to RUNNING and record JOB_STARTED."""

        self._validate_positive_id(job_id, field_name="job_id")

        def operation() -> ProductionJob:
            job = self._get_job_for_update(job_id)
            self._require_status(
                entity="production job",
                current=job.status,
                expected=JobStatus.REQUESTED,
                target=JobStatus.RUNNING,
            )
            job.status = JobStatus.RUNNING
            job.started_at = self._utcnow()
            self._record_event(
                job_id=job.job_id,
                job_step_id=None,
                event_type=EventType.JOB_STARTED,
                message=f"Production job {job.job_code} started.",
            )
            self._session.flush()
            return job

        return self._run_write_transaction(operation, reason="job_started")

    def start_step(self, job_step_id: int) -> JobStep:
        """Start a Step for legacy/local executors without issuing inventory.

        The physical Robot Cell path must call
        :meth:`start_step_after_execution_accepted` instead. Keeping this method
        preserves the existing non-ROS coordinator contract without claiming a
        pre-dispatch state transition is proof of physical material issue.
        """
        return self._start_step(job_step_id, consume_inventory=False)

    def start_step_after_execution_accepted(self, job_step_id: int) -> JobStep:
        """Atomically issue reserved material and mark the accepted Cell Step RUNNING."""
        return self._start_step(job_step_id, consume_inventory=True)

    def start_step_for_test_override(self, job_step_id: int) -> JobStep:
        """Mark one ready Cell Step RUNNING for the bounded segmented-demo seam.

        This creates no Robot Cell ExecutionAttempt or transport command.  The
        durable STEP_STARTED event records that its RUNNING state is synthetic,
        allowing the worker to leave it held rather than treating it as a
        missing real-attempt restart failure.
        """
        return self._start_step(
            job_step_id,
            consume_inventory=True,
            start_event_message=f"{TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX}: Job step {{step_code}} started.",
        )

    def _start_step(
        self,
        job_step_id: int,
        *,
        consume_inventory: bool,
        start_event_message: str | None = None,
    ) -> JobStep:
        self._validate_positive_id(job_step_id, field_name="job_step_id")
        def operation() -> JobStep:
            step = self._get_job_step_for_update(job_step_id)
            job = self._get_job_for_update(step.job_id)
            self._require_execution_job_status(job)
            if job.status is JobStatus.ROOF_READY:
                self._require_pre_roof_inspection_passed(job)
            self._require_status(entity="job step", current=step.status, expected=StepStatus.PENDING, target=StepStatus.RUNNING)
            self._require_step_is_current(step)
            if consume_inventory:
                InventoryReservationService(self._session).consume_step_after_execution_accepted(
                    job_step_id=step.job_step_id
                )
            step.status = StepStatus.RUNNING
            step.started_at = self._utcnow()
            self._record_event(
                job_id=job.job_id,
                job_step_id=step.job_step_id,
                event_type=EventType.STEP_STARTED,
                message=(start_event_message or "Job step {step_code} started.").format(
                    step_code=step.resolved_step_code
                ),
            )
            self._session.flush()
            return step
        return self._run_write_transaction(operation, reason="step_started")

    def complete_step(self, job_step_id: int) -> JobStep:
        """Complete one Step; terminal metadata, not operation name, closes the Job."""
        self._validate_positive_id(job_step_id, field_name="job_step_id")
        def operation() -> JobStep:
            step = self._get_job_step_for_update(job_step_id)
            job = self._get_job_for_update(step.job_id)
            self._require_execution_job_status(job)
            if job.status is JobStatus.ROOF_READY:
                self._require_pre_roof_inspection_passed(job)
            self._require_status(entity="job step", current=step.status, expected=StepStatus.RUNNING, target=StepStatus.COMPLETED)
            step.status = StepStatus.COMPLETED
            step.completed_at = self._utcnow()
            self._record_event(job_id=job.job_id, job_step_id=step.job_step_id, event_type=EventType.STEP_COMPLETED, message=f"Job step {step.resolved_step_code} completed.")
            self._session.flush()
            if step.is_terminal:
                # Only the canonical roof step is held at the durable outbound
                # checkpoint.  Generic PRE_ROOF-gated recipes retain their
                # established terminal-metadata completion behavior.
                if not (step.operation_code == "INSTALL_ROOF" and self._has_selected_pre_roof_gated_stage(job)):
                    self._complete_job_locked(job)
            elif self._all_completion_steps_completed(job):
                if self._has_selected_pre_roof_gated_stage(job):
                    if self._inner_transport_cleanup_complete(job):
                        self._enter_pre_roof_ready_locked(job)
                else:
                    raise InvalidProductionStateTransitionError(entity="recipe terminal", current="MISSING", target="COMPLETED")
            self._session.flush()
            return step
        return self._run_write_transaction(operation, reason="step_completed")

    def fail_step(
        self, job_step_id: int, *, reason: str, error_code: str | None = None
    ) -> JobStep:
        self._validate_positive_id(job_step_id, field_name="job_step_id")
        normalized_reason = self._normalize_reason(reason)
        normalized_error_code = self._normalize_optional_error_code(error_code)
        def operation() -> JobStep:
            step = self._get_job_step_for_update(job_step_id)
            job = self._get_job_for_update(step.job_id)
            self._require_execution_job_status(job)
            self._require_status(entity="job step", current=step.status, expected=StepStatus.RUNNING, target=StepStatus.FAILED)
            step.status = StepStatus.FAILED
            step.failed_at = self._utcnow()
            step.failure_reason = normalized_reason
            self._record_event(job_id=job.job_id, job_step_id=step.job_step_id, event_type=EventType.STEP_FAILED, error_code=normalized_error_code, message=f"Job step {step.resolved_step_code} failed: {normalized_reason}")
            self._fail_job_locked(job, reason=normalized_reason)
            self._session.flush()
            return step
        return self._run_write_transaction(operation, reason="step_failed")

    def complete_job(self, job_id: int) -> ProductionJob:
        """Complete a RUNNING Job only after every JobStep has completed."""

        self._validate_positive_id(job_id, field_name="job_id")

        def operation() -> ProductionJob:
            job = self._get_job_for_update(job_id)
            if job.status not in {JobStatus.RUNNING, JobStatus.ROOF_READY}:
                raise InvalidProductionStateTransitionError(
                    entity="production job", current=job.status.value, target=JobStatus.COMPLETED.value
                )
            if not self._all_completion_steps_completed(job):
                raise InvalidProductionStateTransitionError(
                    entity="production job",
                    current=job.status.value,
                    target=JobStatus.COMPLETED.value,
                )
            self._complete_job_locked(job)
            self._session.flush()
            return job

        return self._run_write_transaction(operation, reason="job_completed")

    def fail_job(self, job_id: int, *, reason: str) -> ProductionJob:
        """Fail a REQUESTED or RUNNING Job and record JOB_FAILED."""

        self._validate_positive_id(job_id, field_name="job_id")
        normalized_reason = self._normalize_reason(reason)

        def operation() -> ProductionJob:
            job = self._get_job_for_update(job_id)
            if job.status not in {JobStatus.REQUESTED, JobStatus.RUNNING}:
                raise InvalidProductionStateTransitionError(
                    entity="production job",
                    current=job.status.value,
                    target=JobStatus.FAILED.value,
                )
            self._fail_job_locked(job, reason=normalized_reason)
            self._session.flush()
            return job

        return self._run_write_transaction(operation, reason="job_failed")

    def cancel_job(self, job_id: int, *, reason: str) -> ProductionJob:
        """Cancel a non-terminal Job and preserve all execution history.

        Cancellation is a Job-level administrative transition. It deliberately
        does not rewrite JobStep, delivery, or execution-attempt states: callers
        that control a live physical process must stop that process through its
        own authority before cancelling the Job.
        """

        self._validate_positive_id(job_id, field_name="job_id")
        normalized_reason = self._normalize_reason(reason)

        def operation() -> ProductionJob:
            job = self._get_job_for_update(job_id)
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise InvalidProductionStateTransitionError(
                    entity="production job",
                    current=job.status.value,
                    target=JobStatus.CANCELED.value,
                )
            job.status = JobStatus.CANCELED
            InventoryReservationService(self._session).release_unused_job_reservations(job=job)
            self._record_event(
                job_id=job.job_id,
                job_step_id=None,
                event_type=EventType.JOB_CANCELED,
                message=f"Production job {job.job_code} canceled: {normalized_reason}",
            )
            self._session.flush()
            return job

        return self._run_write_transaction(operation, reason="job_canceled")

    def _get_job_for_update(self, job_id: int) -> ProductionJob:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
        )
        if job is None:
            raise ProductionJobNotFoundError(f"Production job not found: job_id={job_id}.")
        return job

    def _get_job_step_for_update(self, job_step_id: int) -> JobStep:
        step = self._session.scalar(
            select(JobStep)
            .where(JobStep.job_step_id == job_step_id)
            .with_for_update()
        )
        if step is None:
            raise JobStepNotFoundError(f"Job step not found: job_step_id={job_step_id}.")
        return step

    def _require_step_is_current(self, step: JobStep) -> None:
        active_step_id = self._session.scalar(
            select(JobStep.job_step_id).where(
                JobStep.job_id == step.job_id,
                JobStep.status == StepStatus.RUNNING,
            )
        )
        if active_step_id is not None:
            raise InvalidProductionStateTransitionError(
                entity="job step",
                current=StepStatus.RUNNING.value,
                target=StepStatus.RUNNING.value,
            )

        prior_statuses = [
            candidate.status
            for candidate in self._session.scalars(
                select(JobStep)
                .where(JobStep.job_id == step.job_id)
            )
            if candidate.resolved_step_order < step.resolved_step_order
        ]
        if any(status is not StepStatus.COMPLETED for status in prior_statuses):
            raise InvalidProductionStateTransitionError(
                entity="job step",
                current=step.status.value,
                target=StepStatus.RUNNING.value,
            )

    def _all_completion_steps_completed(self, job: ProductionJob) -> bool:
        statement = select(JobStep.status).where(JobStep.job_id == job.job_id)
        if job.assembly_recipe_id is not None:
            statement = statement.where(JobStep.source_recipe_stage_id.is_not(None))
        statuses = list(self._session.scalars(statement))
        return bool(statuses) and all(status is StepStatus.COMPLETED for status in statuses)

    def _require_execution_job_status(self, job: ProductionJob) -> None:
        if job.status not in {JobStatus.RUNNING, JobStatus.ROOF_READY}:
            raise InvalidProductionStateTransitionError(entity="production job", current=job.status.value, target=JobStatus.RUNNING.value)

    def _has_selected_pre_roof_gated_stage(self, job: ProductionJob) -> bool:
        if job.assembly_recipe_id is None:
            return False
        stages = self._session.scalars(select(AssemblyRecipeStage).where(
            AssemblyRecipeStage.recipe_id == job.assembly_recipe_id,
            AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
        ))
        return any(
            stage.option_code is None
            or (job.roof_option_code is not None and stage.option_code == job.roof_option_code.value)
            for stage in stages
        )

    def _require_pre_roof_inspection_passed(self, job: ProductionJob) -> None:
        inspection = self._session.scalar(select(ProductionInspection).where(
            ProductionInspection.production_job_id == job.job_id,
            ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
        ).order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc()).limit(1))
        gate_open = (
            inspection is not None
            and inspection.status is ProductionInspectionStatus.COMPLETED
            and inspection.result is ProductionInspectionResultCode.PASS
        )
        if not gate_open:
            current = inspection.status.value if inspection is not None else "MISSING"
            raise InvalidProductionStateTransitionError(
                entity="PRE_ROOF inspection",
                current=current,
                target="COMPLETED+PASS",
            )

    def try_enter_pre_roof_ready_after_inner_return(self, job_id: int) -> ProductionJob:
        """Advance only after the durable INNER pallet return releases DROP.

        This is called after a successful physical or synthetic return.  It is
        intentionally idempotent and owns the normal PRE_ROOF_READY event.
        """
        self._validate_positive_id(job_id, field_name="job_id")
        transitioned = False
        try:
            job = self._get_job_for_update(job_id)
            if (job.status is JobStatus.RUNNING
                    and self._all_completion_steps_completed(job)
                    and self._has_selected_pre_roof_gated_stage(job)
                    and self._inner_transport_cleanup_complete(job)):
                self._enter_pre_roof_ready_locked(job)
                transitioned = True
            self._session.flush()
            self._session.commit()
            if transitioned and self._post_commit_callback is not None:
                self._post_commit_callback(job.job_id, "pre_roof_ready_after_inner_return")
            return job
        except Exception:
            self._session.rollback()
            raise

    def _inner_transport_cleanup_complete(self, job: ProductionJob) -> bool:
        deliveries = list(self._session.scalars(select(JobMaterialDelivery).where(
            JobMaterialDelivery.production_job_id == job.job_id,
            JobMaterialDelivery.supply_group_code == "INNER_WALL",
            JobMaterialDelivery.supply_mode == SupplyMode.TRANSPORTED,
            JobMaterialDelivery.status == MaterialDeliveryStatus.COMPLETED,
        )))
        if not deliveries:
            return True
        drop = DropResourceService(self._session).get_drop_state()
        for delivery in deliveries:
            returned = self._session.scalar(select(ExecutionAttempt.attempt_id).where(
                ExecutionAttempt.job_delivery_id == delivery.job_delivery_id,
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
                ExecutionAttempt.status == ExecutionAttemptStatus.SUCCEEDED,
            ).limit(1)) is not None
            if not returned or drop.owner_delivery_id == delivery.job_delivery_id:
                return False
        return True

    def _enter_pre_roof_ready_locked(self, job: ProductionJob) -> None:
        job.status = JobStatus.PRE_ROOF_READY
        job.completed_at = None
        self._ensure_pre_roof_inspection_locked(job)
        self._record_event(job_id=job.job_id, job_step_id=None, event_type=EventType.PRE_ROOF_READY, message=f"Production job {job.job_code} is ready for PRE_ROOF inspection.")

    def _complete_job_locked(self, job: ProductionJob) -> None:
        job.status = JobStatus.COMPLETED
        job.completed_at = self._utcnow()
        self._record_event(job_id=job.job_id, job_step_id=None, event_type=EventType.JOB_COMPLETED, message=f"Production job {job.job_code} completed.")

    def _ensure_pre_roof_inspection_locked(self, job: ProductionJob) -> ProductionInspection:
        inspection = self._session.scalar(
            select(ProductionInspection)
            .where(
                ProductionInspection.production_job_id == job.job_id,
                ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
            )
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )
        if inspection is None:
            inspection = ProductionInspection(
                production_job_id=job.job_id,
                inspection_type=ProductionInspectionType.PRE_ROOF,
                inspection_cycle=1,
                status=ProductionInspectionStatus.PENDING,
                result=None,
                production_valid=False,
                vision_production_valid=False,
                is_passed=None,
            )
            self._session.add(inspection)
        return inspection

    def _fail_job_locked(self, job: ProductionJob, *, reason: str) -> None:
        job.status = JobStatus.FAILED
        job.failed_at = self._utcnow()
        job.failure_reason = reason
        InventoryReservationService(self._session).release_unused_job_reservations(job=job)
        self._record_event(
            job_id=job.job_id,
            job_step_id=None,
            event_type=EventType.JOB_FAILED,
            message=f"Production job {job.job_code} failed: {reason}",
        )

    def _record_event(
        self,
        *,
        job_id: int,
        job_step_id: int | None,
        event_type: EventType,
        message: str,
        error_code: str | None = None,
    ) -> ProductionEvent:
        event = ProductionEvent(
            job_id=job_id,
            job_step_id=job_step_id,
            event_type=event_type,
            error_code=error_code,
            message=message,
        )
        self._session.add(event)
        return event

    def _run_write_transaction(self, operation: Callable[[], _Result], *, reason: str) -> _Result:
        try:
            result = operation()
            self._session.commit()
            if self._post_commit_callback is not None:
                self._post_commit_callback(result.job_id, reason)
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _require_status(
        *,
        entity: str,
        current: JobStatus | StepStatus,
        expected: JobStatus | StepStatus,
        target: JobStatus | StepStatus,
    ) -> None:
        if current is not expected:
            raise InvalidProductionStateTransitionError(
                entity=entity,
                current=current.value,
                target=target.value,
            )

    @staticmethod
    def _validate_positive_id(value: int, *, field_name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidProductionInputError(f"{field_name} must be a positive integer.")

    @staticmethod
    def _normalize_job_code(job_code: str) -> str:
        if not isinstance(job_code, str) or not (normalized := job_code.strip()):
            raise InvalidProductionInputError("job_code must be a non-empty string.")
        if len(normalized) > 80:
            raise InvalidProductionInputError("job_code must be 80 characters or fewer.")
        return normalized

    @staticmethod
    def _normalize_roof_option_code(
        roof_option_code: RoofOptionCode | None,
    ) -> RoofOptionCode | None:
        if roof_option_code is None:
            return None
        if not isinstance(roof_option_code, RoofOptionCode):
            raise InvalidProductionInputError(
                "roof_option_code must be a RoofOptionCode or None."
            )
        return roof_option_code

    @staticmethod
    def _validate_pending_source(
        *, source_pending_request_id: int | None, source_item_index: int | None
    ) -> None:
        if (source_pending_request_id is None) != (source_item_index is None):
            raise InvalidProductionInputError(
                "source_pending_request_id and source_item_index must be set together."
            )
        if source_pending_request_id is not None:
            ProductionOrchestrationService._validate_positive_id(
                source_pending_request_id, field_name="source_pending_request_id"
            )
            ProductionOrchestrationService._validate_positive_id(
                source_item_index, field_name="source_item_index"
            )

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        if not isinstance(reason, str) or not (normalized := reason.strip()):
            raise InvalidProductionInputError("reason must be a non-empty string.")
        return normalized

    @staticmethod
    def _normalize_optional_error_code(error_code: str | None) -> str | None:
        if error_code is None:
            return None
        if not isinstance(error_code, str):
            raise InvalidProductionInputError("error_code must be a string or None.")
        return error_code.strip() or None

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
