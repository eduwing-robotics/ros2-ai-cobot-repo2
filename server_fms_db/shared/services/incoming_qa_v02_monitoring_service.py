"""Read-only Incoming QA v0.2 transaction diagnostics for the production monitor."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shared.models.factory import IncomingQATransaction, MaterialInspection
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from shared.vision_recipe_mapping import IncomingQAInspectionMode
from shared.schemas.production import (
    IncomingQAV02GateResponse,
    IncomingQAV02InspectionItemResponse,
    IncomingQAV02MonitorResponse,
    IncomingQAV02TransactionResponse,
)


class IncomingQAV02MonitoringService:
    """Project persisted v0.2 request/item evidence without lifecycle side effects."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_job_monitor(self, *, job_id: int) -> IncomingQAV02MonitorResponse:
        transactions = list(
            self._session.scalars(
                select(IncomingQATransaction)
                .where(IncomingQATransaction.production_job_id == job_id)
                .options(selectinload(IncomingQATransaction.inspections))
                .order_by(IncomingQATransaction.created_at.asc(), IncomingQATransaction.transaction_id.asc())
            )
        )
        readiness = IncomingQAOrchestrationService(self._session).preproduction_readiness(job_id=job_id)
        test_hold = IncomingQAV02OrchestrationService(self._session).test_hold_state(job_id=job_id)
        return IncomingQAV02MonitorResponse(
            job_id=job_id,
            gate=IncomingQAV02GateResponse(
                status="RELEASE" if readiness.ready else "HOLD",
                total_expected_items=readiness.total_items,
                released_items=readiness.released_items,
            ),
            incoming_qa_test_hold=test_hold.enabled,
            can_enable_incoming_qa_test_hold=test_hold.can_enable,
            can_disable_incoming_qa_test_hold=test_hold.can_disable,
            can_advance_house_b=test_hold.can_advance_house_b,
            can_release_incoming_qa_test_hold=test_hold.can_release,
            transactions=[self._to_transaction(transaction) for transaction in transactions],
        )

    def _to_transaction(self, transaction: IncomingQATransaction) -> IncomingQAV02TransactionResponse:
        requested_items = self._requested_items(transaction.immutable_request_snapshot)
        inspections = {inspection.delivery_item_id: inspection for inspection in transaction.inspections}
        items = [
            self._to_item(
                requested=item,
                inspection=inspections.get(item["delivery_item_id"]),
                transaction_cycle=transaction.inspection_cycle,
            )
            for item in requested_items
        ]
        return IncomingQAV02TransactionResponse(
            transaction_id=transaction.transaction_id,
            inspection_request_id=transaction.inspection_request_id,
            inspection_mode=transaction.inspection_mode,
            inspection_cycle=transaction.inspection_cycle,
            status=transaction.status,
            overall_result=transaction.overall_result,
            production_valid=transaction.production_valid,
            retry_count=transaction.retry_count,
            ack_accepted=transaction.ack_accepted,
            ack_duplicate=transaction.ack_duplicate,
            ack_reason_code=transaction.ack_reason_code,
            error_reason=transaction.error_reason,
            camera_source=transaction.camera_source,
            vision_timestamp=transaction.vision_timestamp,
            model_scope=transaction.model_scope,
            model_version=transaction.model_version,
            created_at=transaction.created_at,
            sent_at=transaction.sent_at,
            acked_at=transaction.acked_at,
            completed_at=transaction.completed_at,
            can_reinspect_mode=self._can_reinspect_mode(transaction),
            items=items,
        )

    def _can_reinspect_mode(self, transaction: IncomingQATransaction) -> bool:
        try:
            mode = IncomingQAInspectionMode(transaction.inspection_mode)
        except ValueError:
            return False
        if not IncomingQAV02OrchestrationService(self._session).can_request_mode_reinspection(
            job_id=transaction.production_job_id,
            inspection_mode=mode,
        ):
            return False
        latest_id = self._session.scalar(
            select(IncomingQATransaction.transaction_id)
            .where(
                IncomingQATransaction.production_job_id == transaction.production_job_id,
                IncomingQATransaction.inspection_mode == mode.value,
            )
            .order_by(
                IncomingQATransaction.inspection_cycle.desc(),
                IncomingQATransaction.transaction_id.desc(),
            )
            .limit(1)
        )
        return latest_id == transaction.transaction_id

    @staticmethod
    def _requested_items(snapshot: str) -> list[dict[str, Any]]:
        """Use the immutable wire context so pending transactions remain visible."""

        try:
            payload = json.loads(snapshot)
            raw_items = payload.get("items") if isinstance(payload, dict) else None
        except (TypeError, json.JSONDecodeError):
            raw_items = None
        if not isinstance(raw_items, list):
            return []
        items = [
            item
            for item in raw_items
            if isinstance(item, dict)
            and isinstance(item.get("slot_id"), str)
            and isinstance(item.get("delivery_item_id"), int)
            and isinstance(item.get("expected_part_code"), str)
            and isinstance(item.get("expected_class_name"), str)
            and isinstance(item.get("expected_quantity"), int)
        ]
        return sorted(items, key=lambda item: (item["slot_id"], item["delivery_item_id"]))

    @staticmethod
    def _to_item(
        *,
        requested: dict[str, Any],
        inspection: MaterialInspection | None,
        transaction_cycle: int,
    ) -> IncomingQAV02InspectionItemResponse:
        detail = IncomingQAV02MonitoringService._detail(inspection.result_detail_json if inspection else None)
        return IncomingQAV02InspectionItemResponse(
            slot_id=requested["slot_id"],
            delivery_item_id=requested["delivery_item_id"],
            expected_part_code=requested["expected_part_code"],
            expected_class_name=requested["expected_class_name"],
            expected_quantity=requested["expected_quantity"],
            inspection_cycle=inspection.inspection_cycle if inspection else transaction_cycle,
            status=inspection.status if inspection else None,
            result=inspection.result if inspection else None,
            production_valid=inspection.production_valid if inspection else None,
            predicted_class_name=inspection.predicted_class_name if inspection else None,
            material_confidence=inspection.material_confidence if inspection else None,
            detected_quantity=inspection.detected_quantity if inspection else None,
            failure_type=inspection.failure_type if inspection else None,
            failure_reason=inspection.failure_reason if inspection else None,
            defects=detail["defects"],
            quality_scores=detail["quality_scores"],
            requested_at=inspection.requested_at if inspection else None,
            completed_at=inspection.completed_at if inspection else None,
        )

    @staticmethod
    def _detail(raw: str | None) -> dict[str, list[str] | dict[str, float] | None]:
        """Expose stable evidence fields only; malformed historical JSON stays non-fatal."""

        try:
            detail = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            detail = {}
        defects_raw = detail.get("defects") if isinstance(detail, dict) else None
        defects = [value for value in defects_raw if isinstance(value, str)] if isinstance(defects_raw, list) else []
        scores_raw = detail.get("quality_scores") if isinstance(detail, dict) else None
        quality_scores = (
            {key: float(value) for key, value in scores_raw.items() if isinstance(key, str) and isinstance(value, (int, float))}
            if isinstance(scores_raw, dict)
            else None
        )
        return {"defects": defects, "quality_scores": quality_scores}
