"""Read-only Delivery and latest Incoming QA monitoring composition."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fms_server.empty_pallet_return_eligibility import EmptyPalletReturnEligibilityService
from fms_server.empty_pallet_return_service import EmptyPalletReturnNotEligibleError
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    DropResourceService,
    DropResourceSnapshot,
    DropResourceState,
)

from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobStatus,
    StepStatus,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    SupplyMode,
)
from shared.schemas.production import (
    IncomingQALatestInspectionResponse,
    IncomingQAMonitoringState,
    JobMaterialDeliveryItemResponse,
    JobMaterialDeliveryResponse,
)
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.terminal_drop_recovery_service import (
    TerminalDropRecoveryError,
    TerminalDropRecoveryService,
)
from shared.services.operator_execution_ready_service import is_robot_cell_execution_step


class MaterialDeliveryMonitoringService:
    """Compose a bounded-query, zero-write operator view for one Job."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._inspections = MaterialInspectionService()

    def get_deliveries_for_job(self, *, job_id: int) -> list[JobMaterialDeliveryResponse]:
        """Return Delivery -> Item -> latest QA only from persisted evidence.

        The method uses persisted Delivery, Item/JobStep, inspection, Attempt,
        and DROP evidence only. It intentionally performs no
        commit, flush, lifecycle transition, dispatch, or network I/O.
        """
        deliveries = list(
            self._session.scalars(
                select(JobMaterialDelivery)
                .options(
                    joinedload(JobMaterialDelivery.feed_execution),
                    joinedload(JobMaterialDelivery.production_job),
                )
                .where(JobMaterialDelivery.production_job_id == job_id)
                .order_by(JobMaterialDelivery.batch_order, JobMaterialDelivery.job_delivery_id)
            )
        )
        if not deliveries:
            return []

        delivery_ids = [delivery.job_delivery_id for delivery in deliveries]
        items = list(
            self._session.scalars(
                select(JobMaterialDeliveryItem)
                .options(joinedload(JobMaterialDeliveryItem.job_step))
                .where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids))
                .order_by(
                    JobMaterialDeliveryItem.job_delivery_id,
                    JobMaterialDeliveryItem.delivery_item_id,
                )
            )
        )
        latest_by_item_id = self._latest_by_item_id(
            item_ids=[item.delivery_item_id for item in items]
        )
        return_attempts_by_delivery_id = self._empty_return_attempts_by_delivery_id(
            delivery_ids=delivery_ids
        )
        drop_state = DropResourceService(self._session).get_drop_state()
        items_by_delivery_id: dict[int, list[JobMaterialDeliveryItem]] = defaultdict(list)
        for item in items:
            items_by_delivery_id[item.job_delivery_id].append(item)

        return [
            self._to_delivery_response(
                delivery=delivery,
                items=items_by_delivery_id[delivery.job_delivery_id],
                latest_by_item_id=latest_by_item_id,
                return_attempts=return_attempts_by_delivery_id.get(delivery.job_delivery_id, []),
                drop_state=drop_state,
            )
            for delivery in deliveries
        ]

    def _empty_return_attempts_by_delivery_id(
        self, *, delivery_ids: list[int]
    ) -> dict[int, list[ExecutionAttempt]]:
        attempts_by_delivery_id: dict[int, list[ExecutionAttempt]] = defaultdict(list)
        attempts = self._session.scalars(
            select(ExecutionAttempt)
            .where(
                ExecutionAttempt.job_delivery_id.in_(delivery_ids),
                ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
                ExecutionAttempt.command_type == EMPTY_RETURN_COMMAND_TYPE,
            )
            .order_by(ExecutionAttempt.job_delivery_id, ExecutionAttempt.attempt_no)
        )
        for attempt in attempts:
            if attempt.job_delivery_id is not None:
                attempts_by_delivery_id[attempt.job_delivery_id].append(attempt)
        return attempts_by_delivery_id

    def _empty_return_state(
        self,
        *,
        delivery: JobMaterialDelivery,
        return_attempts: list[ExecutionAttempt],
        drop_state: DropResourceSnapshot,
    ) -> tuple[str, bool]:
        if delivery.supply_mode is not SupplyMode.TRANSPORTED:
            return "NOT_APPLICABLE", False
        if any(attempt.status is ExecutionAttemptStatus.SUCCEEDED for attempt in return_attempts):
            return "SUCCEEDED", False
        if any(
            attempt.status in {
                ExecutionAttemptStatus.CREATED,
                ExecutionAttemptStatus.DISPATCHING,
                ExecutionAttemptStatus.ACCEPTED,
                ExecutionAttemptStatus.UNKNOWN,
            }
            for attempt in return_attempts
        ):
            return "IN_PROGRESS", False
        if delivery.production_job.status in {
            JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
        }:
            return "NOT_READY", False
        try:
            EmptyPalletReturnEligibilityService(self._session).assert_eligible(
                job_delivery_id=delivery.job_delivery_id,
                production_job_id=delivery.production_job_id,
                lock_rows=False,
            )
        except EmptyPalletReturnNotEligibleError:
            return "NOT_READY", False
        if (
            drop_state.state is not DropResourceState.OCCUPIED
            or drop_state.owner_delivery_id != delivery.job_delivery_id
        ):
            return "NOT_READY", False
        if any(
            attempt.status in {ExecutionAttemptStatus.FAILED, ExecutionAttemptStatus.CANCELED}
            for attempt in return_attempts
        ):
            return "FAILED_RETRY_ELIGIBLE", True
        return "ELIGIBLE", True

    def _terminal_drop_cleanup_state(
        self,
        *,
        delivery: JobMaterialDelivery,
        return_attempts: list[ExecutionAttempt],
        drop_state: DropResourceSnapshot,
    ) -> tuple[bool, bool]:
        """Expose a terminal stranded-pallet condition without changing state."""
        required = (
            delivery.production_job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
            and delivery.supply_mode is SupplyMode.TRANSPORTED
            and delivery.status is MaterialDeliveryStatus.COMPLETED
            and not any(attempt.status is ExecutionAttemptStatus.SUCCEEDED for attempt in return_attempts)
            and drop_state.state is DropResourceState.OCCUPIED
            and drop_state.owner_delivery_id == delivery.job_delivery_id
        )
        if not required:
            return False, False
        try:
            TerminalDropRecoveryService(self._session).assess(
                job_id=delivery.production_job_id,
                delivery_id=delivery.job_delivery_id,
            )
        except TerminalDropRecoveryError:
            return True, False
        return True, True

    def _latest_by_item_id(self, *, item_ids: list[int]) -> dict[int, MaterialInspection]:
        if not item_ids:
            return {}
        inspections = self._session.scalars(
            select(MaterialInspection)
            .where(MaterialInspection.delivery_item_id.in_(item_ids))
            .order_by(
                MaterialInspection.delivery_item_id.asc(),
                MaterialInspection.inspection_cycle.desc(),
            )
        )
        latest: dict[int, MaterialInspection] = {}
        for inspection in inspections:
            latest.setdefault(inspection.delivery_item_id, inspection)
        return latest

    def _to_delivery_response(
        self,
        *,
        delivery: JobMaterialDelivery,
        items: list[JobMaterialDeliveryItem],
        latest_by_item_id: dict[int, MaterialInspection],
        return_attempts: list[ExecutionAttempt],
        drop_state: DropResourceSnapshot,
    ) -> JobMaterialDeliveryResponse:
        item_responses = [
            self._to_item_response(
                item=item,
                latest=latest_by_item_id.get(item.delivery_item_id),
            )
            for item in items
        ]
        qa_released_items = sum(1 for item in item_responses if item.qa_released)
        qa_total_items = len(item_responses)
        empty_return_status, can_empty_pallet_return = self._empty_return_state(
            delivery=delivery,
            return_attempts=return_attempts,
            drop_state=drop_state,
        )
        terminal_drop_cleanup_required, can_terminal_drop_cleanup = self._terminal_drop_cleanup_state(
            delivery=delivery,
            return_attempts=return_attempts,
            drop_state=drop_state,
        )
        can_start_outer_wall_batch = self.__class__._can_start_outer_wall_batch(
            delivery=delivery, items=items
        )
        return JobMaterialDeliveryResponse(
            job_delivery_id=delivery.job_delivery_id,
            batch_order=delivery.batch_order,
            delivery_code=delivery.delivery_code,
            display_name=delivery.display_name,
            status=delivery.status,
            created_at=delivery.created_at,
            started_at=delivery.started_at,
            completed_at=delivery.completed_at,
            failed_at=delivery.failed_at,
            supply_mode=delivery.supply_mode,
            supply_group_code=delivery.supply_group_code,
            supply_destination_code=delivery.supply_destination_code,
            physical_ready=delivery.physical_ready_at is not None,
            physical_ready_at=delivery.physical_ready_at,
            physical_ready_request_id=delivery.physical_ready_request_id,
            manual_prestage_ready_at=delivery.manual_prestage_ready_at,
            manual_prestage_ready=delivery.manual_prestage_ready_at is not None,
            qa_applicable=self._qa_applicable(delivery),
            qa_total_items=qa_total_items,
            qa_released_items=qa_released_items,
            qa_all_released=bool(qa_total_items) and qa_released_items == qa_total_items,
            empty_pallet_return_status=empty_return_status,
            can_empty_pallet_return=can_empty_pallet_return,
            terminal_drop_cleanup_required=terminal_drop_cleanup_required,
            can_terminal_drop_cleanup=can_terminal_drop_cleanup,
            can_start_outer_wall_batch=can_start_outer_wall_batch,
            items=item_responses,
            feed_execution=delivery.feed_execution,
        )


    def _can_start_outer_wall_batch(
        *, delivery: JobMaterialDelivery, items: list[JobMaterialDeliveryItem]
    ) -> bool:
        if (
            delivery.production_job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
            or delivery.supply_group_code != "OUTER_WALLS"
            or delivery.supply_mode is not SupplyMode.TRANSPORTED
            or delivery.status is not MaterialDeliveryStatus.COMPLETED
            or not items
        ):
            return False
        steps = [item.job_step for item in items]
        if any(
            step is None
            or step.supply_group_code != delivery.supply_group_code
            or step.supply_mode is not delivery.supply_mode
            or not is_robot_cell_execution_step(step)
            or step.status not in {StepStatus.PENDING, StepStatus.COMPLETED}
            for step in steps
        ):
            return False
        return any(
            step.status is StepStatus.PENDING and step.operator_execution_ready_at is None
            for step in steps if step is not None
        )

    def _to_item_response(
        self,
        *,
        item: JobMaterialDeliveryItem,
        latest: MaterialInspection | None,
    ) -> JobMaterialDeliveryItemResponse:
        qa_released = self._inspections.is_release_allowed(latest)
        return JobMaterialDeliveryItemResponse(
            delivery_item_id=item.delivery_item_id,
            job_step_id=item.job_step_id,
            part_code=item.part_code,
            # Deferred material can be QA-visible before its runtime JobStep
            # exists.  Part Master remains the authoritative detailed QA class.
            vision_class=(
                item.job_step.vision_class
                if item.job_step is not None
                else (item.part.vision_class if item.part is not None else None)
            ),
            expected_quantity=item.quantity,
            qa_state=self._qa_state(latest=latest, qa_released=qa_released),
            qa_released=qa_released,
            latest_inspection=self._to_latest_inspection(latest),
        )

    @staticmethod
    def _qa_applicable(delivery: JobMaterialDelivery) -> bool | None:
        if delivery.supply_mode in (SupplyMode.TRANSPORTED, SupplyMode.MANUAL):
            return True
        # Legacy carries no new-policy QA contract and must not be displayed as
        # QA=false merely because it has no policy snapshot.
        return None

    @staticmethod
    def _qa_state(
        *, latest: MaterialInspection | None, qa_released: bool
    ) -> IncomingQAMonitoringState:
        if latest is None:
            return IncomingQAMonitoringState.NOT_REQUESTED
        if latest.status is MaterialInspectionStatus.REQUESTED:
            return IncomingQAMonitoringState.REQUESTED
        if latest.status is MaterialInspectionStatus.RUNNING:
            return IncomingQAMonitoringState.RUNNING
        if latest.status is MaterialInspectionStatus.ERROR:
            return IncomingQAMonitoringState.ERROR
        if qa_released:
            return IncomingQAMonitoringState.RELEASED
        if latest.result is MaterialInspectionResult.FAIL:
            return IncomingQAMonitoringState.FAILED
        if latest.result is MaterialInspectionResult.NOT_EVALUATED:
            return IncomingQAMonitoringState.NOT_EVALUATED
        return IncomingQAMonitoringState.NOT_RELEASED

    @staticmethod
    def _to_latest_inspection(
        inspection: MaterialInspection | None,
    ) -> IncomingQALatestInspectionResponse | None:
        if inspection is None:
            return None
        return IncomingQALatestInspectionResponse(
            inspection_request_id=inspection.inspection_request_id,
            inspection_cycle=inspection.inspection_cycle,
            status=inspection.status,
            result=inspection.result,
            production_valid=inspection.production_valid,
            failure_type=inspection.failure_type,
            failure_reason=inspection.failure_reason,
            detected_quantity=inspection.detected_quantity,
            camera_source=inspection.camera_source,
            vision_timestamp=inspection.vision_timestamp,
            requested_at=inspection.requested_at,
            started_at=inspection.started_at,
            completed_at=inspection.completed_at,
        )
