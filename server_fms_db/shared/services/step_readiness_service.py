"""Single FMS-facing boundary for current production Step prerequisites."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from shared.models.factory import JobStep, MaterialDeliveryStatus, MaterialFeedStatus, SupplyMode
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.transport_eligibility_service import TransportEligibilityService
from shared.services.operator_execution_ready_service import operator_execution_ready_required


class StepReadinessReason(StrEnum):
    """Concrete reasons an already selected production Step cannot dispatch."""

    MATERIAL_NOT_READY = "MATERIAL_NOT_READY"
    MATERIAL_FEED_NOT_READY = "MATERIAL_FEED_NOT_READY"
    INCOMING_QA_HOLD = "INCOMING_QA_HOLD"
    POLICY_INVALID = "POLICY_INVALID"
    PHYSICAL_READY_REQUIRED = "PHYSICAL_READY_REQUIRED"
    TRANSPORT_PENDING = "TRANSPORT_PENDING"
    TRANSPORT_IN_PROGRESS = "TRANSPORT_IN_PROGRESS"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    MANUAL_SUPPLY_POLICY_PENDING = "MANUAL_SUPPLY_POLICY_PENDING"
    MANUAL_PRESTAGE_REQUIRED = "MANUAL_PRESTAGE_REQUIRED"
    OPERATOR_EXECUTION_READY_REQUIRED = "OPERATOR_EXECUTION_READY_REQUIRED"
    PRE_PRODUCTION_QA_INCOMPLETE = "PRE_PRODUCTION_QA_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class StepReadinessResult:
    ready: bool
    reason: StepReadinessReason | None = None

    @classmethod
    def ready_now(cls) -> "StepReadinessResult":
        return cls(ready=True)

    @classmethod
    def material_not_ready(cls) -> "StepReadinessResult":
        return cls(ready=False, reason=StepReadinessReason.MATERIAL_NOT_READY)


class StepReadinessService:
    """Evaluate immutable legacy/new-policy material prerequisites.

    Phase 3A deliberately keeps MANUAL fail-closed because its QA applicability
    has not been approved.  It also keeps the historic Feed prerequisite only
    for fully legacy material snapshots.
    """

    def __init__(self, material_delivery_service: MaterialDeliveryService) -> None:
        self._material_delivery_service = material_delivery_service
        self._session = material_delivery_service._session
        self._material_feed_service = MaterialFeedExecutionService(self._session)
        self._transport_qa = TransportEligibilityService(self._session)
        from shared.services.material_inspection_service import MaterialInspectionService

        self._material_inspection_service = MaterialInspectionService()
        from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
        self._preproduction_qa = IncomingQAOrchestrationService(self._session)

    def evaluate(self, *, job_id: int, job_step_id: int) -> StepReadinessResult:
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            raise ValueError("job_id must be a positive integer.")
        if not isinstance(job_step_id, int) or isinstance(job_step_id, bool) or job_step_id <= 0:
            raise ValueError("job_step_id must be a positive integer.")
        step = self._session.get(JobStep, job_step_id)
        if step is None or step.job_id != job_id:
            raise ValueError("job_step_id must belong to job_id.")
        if not self._preproduction_qa.is_preproduction_ready(job_id=job_id):
            return StepReadinessResult(
                False, StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
            )

        policy = self._policy_kind(step)
        if policy is None:
            return StepReadinessResult(False, StepReadinessReason.POLICY_INVALID)
        if policy is SupplyMode.TRANSPORTED:
            return self._evaluate_transported(step)
        if policy is SupplyMode.MANUAL:
            return self._evaluate_manual(step)
        return self._evaluate_legacy(job_step_id)

    def _evaluate_legacy(self, job_step_id: int) -> StepReadinessResult:
        deliveries = self._material_delivery_service.get_required_deliveries_for_step(job_step_id)
        if not self._material_delivery_service.is_material_ready_for_step(job_step_id):
            return StepReadinessResult.material_not_ready()
        for delivery in deliveries:
            feed = self._material_feed_service.get_for_delivery(delivery.job_delivery_id)
            for item in delivery.items:
                if not self._material_inspection_service.is_delivery_item_released(self._session, item.delivery_item_id):
                    return StepReadinessResult(False, StepReadinessReason.INCOMING_QA_HOLD)
            if feed is None or feed.status is not MaterialFeedStatus.COMPLETED:
                return StepReadinessResult(False, StepReadinessReason.MATERIAL_FEED_NOT_READY)
        return StepReadinessResult.ready_now()

    def _evaluate_transported(self, step: JobStep) -> StepReadinessResult:
        deliveries = self._material_delivery_service.get_required_deliveries_for_step(step.job_step_id)
        if not deliveries or any(not self._delivery_matches_step_policy(step, delivery) for delivery in deliveries):
            return StepReadinessResult(False, StepReadinessReason.POLICY_INVALID)
        for delivery in deliveries:
            if not self._transport_qa.are_all_delivery_items_released(delivery):
                return StepReadinessResult(False, StepReadinessReason.INCOMING_QA_HOLD)
            if delivery.physical_ready_at is None:
                return StepReadinessResult(False, StepReadinessReason.PHYSICAL_READY_REQUIRED)
            if delivery.status is MaterialDeliveryStatus.PENDING:
                return StepReadinessResult(False, StepReadinessReason.TRANSPORT_PENDING)
            if delivery.status is MaterialDeliveryStatus.IN_PROGRESS:
                return StepReadinessResult(False, StepReadinessReason.TRANSPORT_IN_PROGRESS)
            if delivery.status is MaterialDeliveryStatus.FAILED:
                return StepReadinessResult(False, StepReadinessReason.TRANSPORT_FAILED)
            if delivery.status is not MaterialDeliveryStatus.COMPLETED:
                return StepReadinessResult(False, StepReadinessReason.POLICY_INVALID)
        if operator_execution_ready_required(step):
            return StepReadinessResult(
                False, StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED
            )
        return StepReadinessResult.ready_now()

    def _evaluate_manual(self, step: JobStep) -> StepReadinessResult:
        deliveries = self._material_delivery_service.get_required_deliveries_for_step(step.job_step_id)
        if not deliveries or any(not self._delivery_matches_step_policy(step, delivery) for delivery in deliveries):
            return StepReadinessResult(False, StepReadinessReason.POLICY_INVALID)
        for delivery in deliveries:
            # Base MANUAL supply has the same authoritative incoming-QA release
            # proof as transported material, but its at-cell human prestage is a
            # distinct durable fact and never a TurtleBot delivery transition.
            if not self._transport_qa.are_all_delivery_items_released(delivery):
                return StepReadinessResult(False, StepReadinessReason.INCOMING_QA_HOLD)
            if delivery.manual_prestage_ready_at is None:
                return StepReadinessResult(False, StepReadinessReason.MANUAL_PRESTAGE_REQUIRED)
        return StepReadinessResult.ready_now()

    @staticmethod
    def _policy_kind(step: JobStep) -> SupplyMode | str | None:
        values = (step.supply_mode, step.supply_group_code, step.supply_destination_code)
        if all(value is None for value in values):
            return "LEGACY"
        if not isinstance(step.supply_mode, SupplyMode):
            return None
        if not isinstance(step.supply_group_code, str) or not step.supply_group_code.strip():
            return None
        return step.supply_mode

    @staticmethod
    def _delivery_matches_step_policy(step: JobStep, delivery) -> bool:
        return (
            delivery.supply_mode is step.supply_mode
            and delivery.supply_group_code == step.supply_group_code
            and delivery.supply_destination_code == step.supply_destination_code
        )
