"""Policy-filtered, idempotent preparation for future Incoming QA dispatch.

This coordinator intentionally has no production lifecycle caller yet.  It
prepares/reuses one item-level transaction; a later caller chooses Vision
sequencing without changing cycle-idempotency semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.incoming_material_qa_http_client import IncomingMaterialQAAcknowledgement
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionStatus,
    SupplyMode,
)
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.material_inspection_service import MaterialInspectionService


class IncomingMaterialQADispatchAction(StrEnum):
    CREATE_NEW = "CREATE_NEW"
    REUSE_EXISTING = "REUSE_EXISTING"
    SKIP_RELEASED = "SKIP_RELEASED"
    HOLD_TERMINAL_FAILURE = "HOLD_TERMINAL_FAILURE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    POLICY_INVALID = "POLICY_INVALID"
    GLOBAL_BUSY = "GLOBAL_BUSY"
    CREATE_REINSPECTION = "CREATE_REINSPECTION"


@dataclass(frozen=True, slots=True)
class IncomingMaterialQADispatchDecision:
    action: IncomingMaterialQADispatchAction
    delivery_item_id: int
    inspection_request_id: str | None = None
    inspection_cycle: int | None = None
    should_send: bool = False


@dataclass(frozen=True, slots=True)
class IncomingMaterialQADispatchResult:
    decision: IncomingMaterialQADispatchDecision
    acknowledgement: IncomingMaterialQAAcknowledgement | None = None


class IncomingMaterialQADispatchCoordinator:
    """Prepare/reuse QA work for one persisted policy-TRANSPORTED DeliveryItem.

    `prepare_inspection()` has no network I/O.  It commits a new immutable
    request before returning.  `dispatch_item()` sends only after this durable
    boundary; neither method is wired to materialization, Worker ticks, or
    callbacks in this foundation phase.
    """

    def __init__(
        self,
        session: Session,
        *,
        inspection_service: MaterialInspectionService | None = None,
        runtime: IncomingMaterialQARuntime | None = None,
    ) -> None:
        self._session = session
        self._inspections = inspection_service or MaterialInspectionService()
        self._runtime = runtime or IncomingMaterialQARuntime(
            inspection_service=self._inspections
        )
        self._global_qa = IncomingQAOrchestrationService(session)

    def prepare_inspection(
        self, *, delivery_item_id: int
    ) -> IncomingMaterialQADispatchDecision:
        """Atomically decide/create/reuse one QA cycle without Vision I/O."""
        try:
            self._global_qa.acquire_global_transaction_guard()
            item = self._inspections.get_delivery_item_for_update(
                self._session, delivery_item_id
            )
            if item is None:
                raise ValueError(f"Delivery item {delivery_item_id} not found.")
            delivery = self._session.execute(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == item.job_delivery_id)
                .with_for_update()
            ).scalar_one_or_none()

            policy_action = self._policy_action(delivery)
            if policy_action is not None:
                self._session.commit()
                return IncomingMaterialQADispatchDecision(
                    action=policy_action,
                    delivery_item_id=delivery_item_id,
                )

            latest = self._inspections.get_latest_inspection(
                self._session, delivery_item_id, for_update=True
            )
            if latest is not None:
                # The idempotent resend exception belongs only to the one
                # globally active transaction.  Historical inconsistent data
                # must not permit a second active Vision dispatch.
                if self._is_active(latest) and self._global_qa.get_active_inspection(
                    excluding_delivery_item_id=delivery_item_id
                ) is not None:
                    self._session.commit()
                    return IncomingMaterialQADispatchDecision(
                        action=IncomingMaterialQADispatchAction.GLOBAL_BUSY,
                        delivery_item_id=delivery_item_id,
                    )
                decision = self._decision_for_latest(item, latest)
                self._session.commit()
                return decision
            if self._global_qa.get_active_inspection() is not None:
                self._session.commit()
                return IncomingMaterialQADispatchDecision(
                    action=IncomingMaterialQADispatchAction.GLOBAL_BUSY,
                    delivery_item_id=delivery_item_id,
                )
            request = self._inspections.request_inspection(
                self._session, delivery_item_id
            )
            self._session.commit()
            return IncomingMaterialQADispatchDecision(
                action=IncomingMaterialQADispatchAction.CREATE_NEW,
                delivery_item_id=delivery_item_id,
                inspection_request_id=request.inspection_request_id,
                inspection_cycle=request.inspection_cycle,
                should_send=True,
            )
        except Exception:
            self._session.rollback()
            raise

    def prepare_reinspection(
        self, *, delivery_item_id: int
    ) -> IncomingMaterialQADispatchDecision:
        """Explicitly allocate a fresh cycle only after FAIL/NOT_EVALUATED."""
        try:
            self._global_qa.acquire_global_transaction_guard()
            item = self._inspections.get_delivery_item_for_update(
                self._session, delivery_item_id
            )
            if item is None:
                raise ValueError(f"Delivery item {delivery_item_id} not found.")
            delivery = self._session.execute(
                select(JobMaterialDelivery)
                .where(JobMaterialDelivery.job_delivery_id == item.job_delivery_id)
                .with_for_update()
            ).scalar_one_or_none()
            policy_action = self._policy_action(delivery)
            if policy_action is not None:
                self._session.commit()
                return IncomingMaterialQADispatchDecision(policy_action, delivery_item_id)
            latest = self._inspections.get_latest_inspection(
                self._session, delivery_item_id, for_update=True
            )
            if latest is None:
                self._session.commit()
                return IncomingMaterialQADispatchDecision(
                    IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE,
                    delivery_item_id,
                )
            if self._is_active(latest):
                if self._global_qa.get_active_inspection(
                    excluding_delivery_item_id=delivery_item_id
                ) is not None:
                    self._session.commit()
                    return IncomingMaterialQADispatchDecision(
                        IncomingMaterialQADispatchAction.GLOBAL_BUSY,
                        delivery_item_id,
                    )
                decision = self._decision_for_latest(item, latest)
                self._session.commit()
                return decision
            if self._inspections.is_release_allowed(latest):
                self._session.commit()
                return self._decision_for_latest(item, latest)
            if not self._is_reinspectable_terminal(latest):
                self._session.commit()
                return self._decision_for_latest(item, latest)
            if self._global_qa.get_active_inspection() is not None:
                self._session.commit()
                return IncomingMaterialQADispatchDecision(
                    IncomingMaterialQADispatchAction.GLOBAL_BUSY,
                    delivery_item_id,
                )
            request = self._inspections.request_inspection(self._session, delivery_item_id)
            self._session.commit()
            return IncomingMaterialQADispatchDecision(
                action=IncomingMaterialQADispatchAction.CREATE_REINSPECTION,
                delivery_item_id=delivery_item_id,
                inspection_request_id=request.inspection_request_id,
                inspection_cycle=request.inspection_cycle,
                should_send=True,
            )
        except Exception:
            self._session.rollback()
            raise

    def dispatch_item(self, *, delivery_item_id: int) -> IncomingMaterialQADispatchResult:
        return self._dispatch(self.prepare_inspection(delivery_item_id=delivery_item_id))

    def dispatch_reinspection_item(
        self, *, delivery_item_id: int
    ) -> IncomingMaterialQADispatchResult:
        return self._dispatch(self.prepare_reinspection(delivery_item_id=delivery_item_id))

    def _dispatch(
        self, decision: IncomingMaterialQADispatchDecision
    ) -> IncomingMaterialQADispatchResult:
        if not decision.should_send:
            return IncomingMaterialQADispatchResult(decision=decision)
        assert decision.inspection_request_id is not None
        acknowledgement = self._runtime.send_existing(
            self._session,
            inspection_request_id=decision.inspection_request_id,
        )
        return IncomingMaterialQADispatchResult(
            decision=decision,
            acknowledgement=acknowledgement,
        )

    @staticmethod
    def _is_active(latest: MaterialInspection) -> bool:
        return latest.status in (
            MaterialInspectionStatus.REQUESTED,
            MaterialInspectionStatus.RUNNING,
        )

    @staticmethod
    def _is_reinspectable_terminal(latest: MaterialInspection) -> bool:
        return (
            latest.status is MaterialInspectionStatus.COMPLETED
            and latest.result is not None
            and latest.result.value in ("FAIL", "NOT_EVALUATED")
        )

    def _decision_for_latest(
        self,
        item: JobMaterialDeliveryItem,
        latest: MaterialInspection,
    ) -> IncomingMaterialQADispatchDecision:
        if latest.status is MaterialInspectionStatus.REQUESTED:
            return IncomingMaterialQADispatchDecision(
                action=IncomingMaterialQADispatchAction.REUSE_EXISTING,
                delivery_item_id=item.delivery_item_id,
                inspection_request_id=latest.inspection_request_id,
                inspection_cycle=latest.inspection_cycle,
                should_send=True,
            )
        if latest.status is MaterialInspectionStatus.RUNNING:
            return IncomingMaterialQADispatchDecision(
                action=IncomingMaterialQADispatchAction.REUSE_EXISTING,
                delivery_item_id=item.delivery_item_id,
                inspection_request_id=latest.inspection_request_id,
                inspection_cycle=latest.inspection_cycle,
            )
        if self._inspections.is_release_allowed(latest):
            return IncomingMaterialQADispatchDecision(
                action=IncomingMaterialQADispatchAction.SKIP_RELEASED,
                delivery_item_id=item.delivery_item_id,
                inspection_request_id=latest.inspection_request_id,
                inspection_cycle=latest.inspection_cycle,
            )
        return IncomingMaterialQADispatchDecision(
            action=IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE,
            delivery_item_id=item.delivery_item_id,
            inspection_request_id=latest.inspection_request_id,
            inspection_cycle=latest.inspection_cycle,
        )

    @staticmethod
    def _policy_action(
        delivery: JobMaterialDelivery | None,
    ) -> IncomingMaterialQADispatchAction | None:
        if delivery is None:
            return IncomingMaterialQADispatchAction.POLICY_INVALID
        values = (
            delivery.supply_mode,
            delivery.supply_group_code,
            delivery.supply_destination_code,
        )
        if all(value is None for value in values):
            return IncomingMaterialQADispatchAction.NOT_APPLICABLE
        if (
            not isinstance(delivery.supply_mode, SupplyMode)
            or not isinstance(delivery.supply_group_code, str)
            or not delivery.supply_group_code.strip()
        ):
            return IncomingMaterialQADispatchAction.POLICY_INVALID
        if delivery.supply_mode in (SupplyMode.TRANSPORTED, SupplyMode.MANUAL):
            # Operator initiation remains explicit; this only declares a valid
            # new-policy item eligible for the reusable QA coordinator.
            return None
        return IncomingMaterialQADispatchAction.POLICY_INVALID
