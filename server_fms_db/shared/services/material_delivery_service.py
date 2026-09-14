from datetime import datetime, timezone
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from shared.realtime.production_events import get_production_change_callback

from shared.models.factory import (
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStep,
    MaterialDeliveryStatus,
    ProductionJob,
    SupplyMode,
)


class MaterialDeliveryError(RuntimeError):
    pass


class MaterialDeliveryNotFoundError(MaterialDeliveryError):
    pass


class MaterialDeliveryConfigurationError(MaterialDeliveryError):
    """Raised when immutable material logistics snapshots are inconsistent."""

    pass


class InvalidMaterialDeliveryStateTransitionError(MaterialDeliveryError):
    pass


class MaterialDeliveryService:
    """Dynamically instantiate material delivery requirements from JobSteps."""

    def __init__(self, session: Session, *, post_commit_callback=None) -> None:
        self._session = session
        self._post_commit_callback = post_commit_callback or get_production_change_callback()

    def instantiate_for_job(self, *, job: ProductionJob) -> list[JobMaterialDelivery]:
        """Create legacy or policy-grouped Delivery rows from immutable JobSteps.

        NULL policy on every material Step remains the legacy consolidated flow.
        A policy on every material Step selects the grouped flow.  Mixing the two
        is a configuration error rather than an implicit MANUAL default.
        """
        material_steps = self._material_steps_for_job(job)
        if not material_steps:
            return []

        # Serialise creation for one job even when more than one caller retries.
        self._session.execute(
            select(ProductionJob.job_id)
            .where(ProductionJob.job_id == job.job_id)
            .with_for_update()
        )
        has_policy = [self._step_has_any_policy(step) for step in material_steps]
        if not any(has_policy):
            return self._instantiate_legacy(job=job, material_steps=material_steps)
        if not all(has_policy):
            raise MaterialDeliveryConfigurationError(
                "Material JobSteps cannot mix legacy NULL logistics policy with configured policy."
            )
        return self._instantiate_grouped(job=job, material_steps=material_steps)


    def instantiate_for_job_steps(
        self, *, job: ProductionJob, material_steps: list[JobStep]
    ) -> list[JobMaterialDelivery]:
        """Materialize a delayed, already-snapshotted policy group for one Job.

        This is intentionally narrower than the initial all-Job path: a legacy
        historical Delivery may already exist, while a later gated policy stage
        must receive its own configured Delivery rather than corrupting it.
        """
        selected = [
            step for step in material_steps
            if step.job_id == job.job_id and step.part_code and step.quantity and step.quantity > 0
        ]
        if len(selected) != len(material_steps):
            raise MaterialDeliveryConfigurationError(
                "Delayed materialization requires material JobSteps belonging to the Job."
            )
        if not selected:
            return []
        if not all(self._step_has_any_policy(step) for step in selected):
            raise MaterialDeliveryConfigurationError(
                "Delayed material JobSteps require a complete configured logistics policy."
            )
        self._session.execute(
            select(ProductionJob.job_id).where(ProductionJob.job_id == job.job_id).with_for_update()
        )
        return self._instantiate_grouped(
            job=job, material_steps=selected, allow_existing_legacy=True,
            allow_existing_unselected_groups=True,
        )

    def pre_materialize_gated_stage_materials(
        self, *, job: ProductionJob, stages: list[AssemblyRecipeStage]
    ) -> list[JobMaterialDelivery]:
        """Create deferred material items for selected PRE_ROOF_PASS stages."""
        selected = self._selected_pre_roof_material_stages(job=job, stages=stages)
        if not selected:
            return []
        self._session.execute(select(ProductionJob.job_id).where(ProductionJob.job_id == job.job_id).with_for_update())
        existing = self._existing_deliveries(job.job_id)
        by_group = {delivery.supply_group_code: delivery for delivery in existing if delivery.supply_group_code}
        created: list[JobMaterialDelivery] = []
        grouped: dict[str, list[AssemblyRecipeStage]] = {}
        for stage in selected:
            _, group, _ = self._policy_from_deferred_stage(stage)
            grouped.setdefault(group, []).append(stage)
        for group, group_stages in grouped.items():
            mode, _, destination = self._policy_from_deferred_stage(group_stages[0])
            if any(self._policy_from_deferred_stage(stage) != (mode, group, destination) for stage in group_stages):
                raise MaterialDeliveryConfigurationError("Gated stages sharing a supply group have conflicting logistics policy.")
            delivery = by_group.get(group)
            expected = {(stage.recipe_stage_id, None, stage.part_code, stage.quantity) for stage in group_stages}
            if delivery is None:
                batch_order = max((row.batch_order for row in existing), default=0) + 1
                delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=batch_order, delivery_code=f"{job.job_code}-DEL-{batch_order}", display_name=f"Material Delivery {group} for {job.job_code}", status=MaterialDeliveryStatus.PENDING, supply_mode=mode, supply_group_code=group, supply_destination_code=destination)
                self._session.add(delivery)
                self._session.flush()
                self._session.add_all(JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=None, source_recipe_stage_id=stage.recipe_stage_id, part_code=stage.part_code, quantity=stage.quantity) for stage in group_stages)
                self._session.flush()
                existing.append(delivery)
                by_group[group] = delivery
                created.append(delivery)
                continue
            if delivery.supply_mode is not mode or delivery.supply_destination_code != destination:
                raise MaterialDeliveryConfigurationError("Pre-materialized gated Delivery conflicts with RecipeStage logistics policy.")
            actual = {(item.source_recipe_stage_id, item.job_step_id, item.part_code, item.quantity) for item in delivery.items}
            if actual != expected:
                raise MaterialDeliveryConfigurationError("Deferred DeliveryItems conflict with selected gated RecipeStages.")
            created.append(delivery)
        return created

    def bind_pre_materialized_gated_stage_item(
        self, *, job: ProductionJob, stage: AssemblyRecipeStage, job_step: JobStep
    ) -> JobMaterialDeliveryItem | None:
        """Bind a deferred material item to its generic runtime JobStep."""
        if job.assembly_recipe_id is None or stage.recipe_id != job.assembly_recipe_id or job_step.job_id != job.job_id or job_step.source_recipe_stage_id != stage.recipe_stage_id or stage.execution_gate != "PRE_ROOF_PASS":
            raise MaterialDeliveryConfigurationError("Deferred binding requires the Job selected gated RecipeStage and its runtime JobStep.")
        if stage.part_code is None or stage.quantity is None:
            return None
        items = list(self._session.scalars(select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(
                JobMaterialDelivery.production_job_id == job.job_id,
                JobMaterialDeliveryItem.source_recipe_stage_id == stage.recipe_stage_id,
            )
            .with_for_update()))
        if len(items) != 1:
            raise MaterialDeliveryConfigurationError("Expected exactly one deferred DeliveryItem for the gated RecipeStage.")
        item = items[0]
        if item.job_step_id is None:
            item.job_step_id = job_step.job_step_id
            self._session.flush()
            return item
        if item.job_step_id == job_step.job_step_id:
            return item
        raise MaterialDeliveryConfigurationError("Deferred DeliveryItem is already bound to a different JobStep.")


    @staticmethod
    def _selected_pre_roof_material_stages(*, job: ProductionJob, stages: list[AssemblyRecipeStage]) -> list[AssemblyRecipeStage]:
        selected: list[AssemblyRecipeStage] = []
        for stage in stages:
            if stage.recipe_id != job.assembly_recipe_id or stage.execution_gate != "PRE_ROOF_PASS":
                continue
            if stage.option_code is not None and (job.roof_option_code is None or stage.option_code != job.roof_option_code.value):
                continue
            if stage.part_code is None and stage.quantity is None:
                continue
            if not isinstance(stage.part_code, str) or not stage.part_code.strip() or stage.quantity is None or stage.quantity <= 0:
                raise MaterialDeliveryConfigurationError("Material-bearing gated RecipeStage requires part_code and positive quantity.")
            selected.append(stage)
        return sorted(selected, key=lambda stage: stage.stage_order)


    @staticmethod
    def _policy_from_deferred_stage(
        stage: AssemblyRecipeStage,
    ) -> tuple[SupplyMode, str, str | None]:
        mode = stage.supply_mode
        group = stage.supply_group_code.strip() if isinstance(stage.supply_group_code, str) else ""
        destination = (
            stage.supply_destination_code.strip()
            if isinstance(stage.supply_destination_code, str)
            else None
        )
        if not isinstance(mode, SupplyMode) or not group:
            raise MaterialDeliveryConfigurationError(
                "Deferred PRE_ROOF roof RecipeStage requires SupplyMode and a non-blank supply_group_code."
            )
        return mode, group, destination or None

    @staticmethod
    def _material_steps_for_job(job: ProductionJob) -> list[JobStep]:
        material_steps: list[JobStep] = []
        for step in job.steps:
            if step.part_code is None or step.quantity is None or step.quantity <= 0:
                continue
            if step.roof_option_code is not None and step.roof_option_code != job.roof_option_code:
                continue
            material_steps.append(step)
        return material_steps


    @staticmethod
    def _step_has_any_policy(step: JobStep) -> bool:
        return any(
            value is not None
            for value in (step.supply_mode, step.supply_group_code, step.supply_destination_code)
        )


    @staticmethod
    def _policy_from_step(step: JobStep) -> tuple[SupplyMode, str, str | None]:
        mode = step.supply_mode
        group = step.supply_group_code.strip() if isinstance(step.supply_group_code, str) else ""
        destination = (
            step.supply_destination_code.strip()
            if isinstance(step.supply_destination_code, str)
            else None
        )
        if not isinstance(mode, SupplyMode) or not group:
            raise MaterialDeliveryConfigurationError(
                "Configured material JobSteps require SupplyMode and a non-blank supply_group_code."
            )
        if destination == "":
            destination = None
        return mode, group, destination

    def _existing_deliveries(self, job_id: int) -> list[JobMaterialDelivery]:
        return list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .options(selectinload(JobMaterialDelivery.items))
                .where(JobMaterialDelivery.production_job_id == job_id)
                .order_by(JobMaterialDelivery.batch_order, JobMaterialDelivery.job_delivery_id)
            )
        )

    def _instantiate_legacy(
        self, *, job: ProductionJob, material_steps: list[JobStep]
    ) -> list[JobMaterialDelivery]:
        existing = self._existing_deliveries(job.job_id)
        if existing:
            if len(existing) != 1 or any(
                value is not None
                for value in (
                    existing[0].supply_mode,
                    existing[0].supply_group_code,
                    existing[0].supply_destination_code,
                )
            ):
                raise MaterialDeliveryConfigurationError(
                    "Legacy material JobSteps conflict with existing grouped Delivery rows."
                )
            self._assert_exact_items(existing[0], material_steps)
            self._ensure_feed_rows(existing)
            return existing

        delivery = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code=f"{job.job_code}-DEL-1",
            display_name=f"Material Delivery for {job.job_code}",
            status=MaterialDeliveryStatus.PENDING,
        )
        self._session.add(delivery)
        self._session.flush()
        self._add_delivery_items(delivery, material_steps)
        self._ensure_feed_rows([delivery])
        return [delivery]

    def _instantiate_grouped(
        self, *,
        job: ProductionJob,
        material_steps: list[JobStep],
        allow_existing_legacy: bool = False,
        allow_existing_unselected_groups: bool = False,
    ) -> list[JobMaterialDelivery]:
        grouped: dict[str, tuple[SupplyMode, str | None, list[JobStep]]] = {}
        for step in material_steps:
            mode, group, destination = self._policy_from_step(step)
            current = grouped.get(group)
            if current is None:
                grouped[group] = (mode, destination, [step])
                continue
            current_mode, current_destination, current_steps = current
            if current_mode is not mode or current_destination != destination:
                raise MaterialDeliveryConfigurationError(
                    f"Supply group {group!r} has conflicting supply_mode or supply_destination_code."
                )
            current_steps.append(step)

        existing = self._existing_deliveries(job.job_id)
        existing_by_group: dict[str, JobMaterialDelivery] = {}
        for delivery in existing:
            if delivery.supply_group_code is None:
                if allow_existing_legacy:
                    continue
                raise MaterialDeliveryConfigurationError(
                    "Configured material JobSteps conflict with an existing legacy Delivery row."
                )
            if delivery.supply_group_code in existing_by_group:
                raise MaterialDeliveryConfigurationError(
                    "Existing Delivery rows contain duplicate supply_group_code values."
                )
            existing_by_group[delivery.supply_group_code] = delivery
        if not allow_existing_unselected_groups and set(existing_by_group) - set(grouped):
            raise MaterialDeliveryConfigurationError(
                "Existing Delivery groups do not match the immutable JobStep policy snapshot."
            )

        deliveries: list[JobMaterialDelivery] = []
        for batch_order, group in enumerate(
            sorted(grouped, key=lambda group: (min(step.step_order for step in grouped[group][2]), group)),
            start=1,
        ):
            mode, destination, steps = grouped[group]
            delivery = existing_by_group.get(group)
            if delivery is None:
                delivery = JobMaterialDelivery(
                    production_job_id=job.job_id,
                    batch_order=batch_order,
                    delivery_code=f"{job.job_code}-DEL-{batch_order}",
                    display_name=f"Material Delivery {group} for {job.job_code}",
                    status=MaterialDeliveryStatus.PENDING,
                    supply_mode=mode,
                    supply_group_code=group,
                    supply_destination_code=destination,
                )
                self._session.add(delivery)
                self._session.flush()
                self._add_delivery_items(delivery, steps)
            else:
                if (
                    delivery.supply_mode is not mode
                    or delivery.supply_destination_code != destination
                ):
                    raise MaterialDeliveryConfigurationError(
                        f"Existing Delivery group {group!r} conflicts with the JobStep policy snapshot."
                    )
                self._assert_exact_items(delivery, steps)
            deliveries.append(delivery)

        # Phase 3A: policy-based groups deliberately have no Feed lifecycle.
        # Existing stale Feed rows are retained but ignored by readiness/Worker.
        return deliveries

    def _add_delivery_items(
        self, delivery: JobMaterialDelivery, material_steps: list[JobStep]
    ) -> None:
        self._session.add_all(
            JobMaterialDeliveryItem(
                job_delivery_id=delivery.job_delivery_id,
                job_step_id=step.job_step_id,
                source_recipe_stage_id=step.source_recipe_stage_id,
                part_code=step.part_code,
                quantity=step.quantity,
            )
            for step in material_steps
        )
        self._session.flush()


    @staticmethod
    def _assert_exact_items(
        delivery: JobMaterialDelivery, material_steps: list[JobStep]
    ) -> None:
        expected = {(step.job_step_id, step.part_code, step.quantity) for step in material_steps}
        actual = {(item.job_step_id, item.part_code, item.quantity) for item in delivery.items}
        if actual != expected:
            raise MaterialDeliveryConfigurationError(
                "Existing DeliveryItems do not match the immutable JobStep material snapshot."
            )

    def _ensure_feed_rows(self, deliveries: list[JobMaterialDelivery]) -> None:
        missing = [
            delivery
            for delivery in deliveries
            if self._session.scalar(
                select(JobMaterialFeedExecution.feed_execution_id).where(
                    JobMaterialFeedExecution.job_delivery_id == delivery.job_delivery_id
                )
            )
            is None
        ]
        if missing:
            from shared.services.material_feed_execution_service import MaterialFeedExecutionService

            MaterialFeedExecutionService(self._session).instantiate_for_deliveries(missing)

    def get_deliveries_for_job(self, job_id: int) -> list[JobMaterialDelivery]:
        return list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .options(selectinload(JobMaterialDelivery.items))
                .where(JobMaterialDelivery.production_job_id == job_id)
                .order_by(JobMaterialDelivery.batch_order)
            )
        )

    def get_required_deliveries_for_step(self, job_step_id: int) -> list[JobMaterialDelivery]:
        # A step implicitly requires the delivery that contains its item
        return list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .join(JobMaterialDeliveryItem)
                .where(JobMaterialDeliveryItem.job_step_id == job_step_id)
                .order_by(JobMaterialDelivery.batch_order)
            )
        )

    def is_material_ready_for_step(self, job_step_id: int) -> bool:
        required = self.get_required_deliveries_for_step(job_step_id)
        if not required:
            return True
        return all(delivery.status is MaterialDeliveryStatus.COMPLETED for delivery in required)

    def start_delivery(self, job_delivery_id: int) -> JobMaterialDelivery:
        return self._transition(job_delivery_id, MaterialDeliveryStatus.PENDING, MaterialDeliveryStatus.IN_PROGRESS)

    def complete_delivery(self, job_delivery_id: int) -> JobMaterialDelivery:
        return self._transition(job_delivery_id, MaterialDeliveryStatus.IN_PROGRESS, MaterialDeliveryStatus.COMPLETED)

    def complete_pending_delivery_for_test_override_in_current_transaction(
        self, job_delivery_id: int
    ) -> JobMaterialDelivery:
        """Use the normal Delivery state transitions without committing.

        The bounded Test Override transport helper owns the surrounding
        transaction so successful synthetic transport evidence and Delivery
        completion become visible together.
        """
        self._transition(
            job_delivery_id,
            MaterialDeliveryStatus.PENDING,
            MaterialDeliveryStatus.IN_PROGRESS,
            commit=False,
        )
        return self._transition(
            job_delivery_id,
            MaterialDeliveryStatus.IN_PROGRESS,
            MaterialDeliveryStatus.COMPLETED,
            commit=False,
        )

    def fail_delivery(self, job_delivery_id: int, reason: str | None = None) -> JobMaterialDelivery:
        return self._transition(job_delivery_id, MaterialDeliveryStatus.IN_PROGRESS, MaterialDeliveryStatus.FAILED, reason=reason)

    def _notify_production_changed(self, job_id: int, reason: str) -> None:
        callback = self._post_commit_callback
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:
            logging.getLogger(__name__).exception("Production post-commit notification failed job_id=%s reason=%s", job_id, reason)


    def _transition(
        self, job_delivery_id: int, expected: MaterialDeliveryStatus, target: MaterialDeliveryStatus, reason: str | None = None, *, commit: bool = True
    ) -> JobMaterialDelivery:
        try:
            delivery = self._session.scalar(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == job_delivery_id)
                .with_for_update()
            )
            if delivery is None:
                raise MaterialDeliveryNotFoundError(
                    f"Job material delivery not found: job_delivery_id={job_delivery_id}."
                )
            if delivery.status is not expected:
                raise InvalidMaterialDeliveryStateTransitionError(
                    f"Invalid job material delivery transition: {delivery.status.value} -> {target.value}."
                )
            delivery.status = target
            now = datetime.now(timezone.utc)
            if target is MaterialDeliveryStatus.IN_PROGRESS:
                delivery.started_at = now
            elif target is MaterialDeliveryStatus.COMPLETED:
                delivery.completed_at = now
            else:
                delivery.failed_at = now
                if reason is not None:
                    delivery.failure_reason = reason
            if commit:
                self._session.commit()
                if target is MaterialDeliveryStatus.COMPLETED:
                    self._notify_production_changed(delivery.production_job_id, "material_delivery_completed")
            return delivery
        except Exception:
            self._session.rollback()
            raise
