"""Durable Incoming Material QA transaction and HOLD/RELEASE policy."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models.factory import (
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionFailureType,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
)
from shared.schemas.vision import IncomingMaterialQARequest, IncomingMaterialQAResult

logger = logging.getLogger(__name__)


class MaterialInspectionError(Exception):
    """Base exception for MaterialInspectionService."""


class MaterialInspectionNotFoundError(MaterialInspectionError):
    """A Vision result referenced no persisted FMS inspection transaction."""


class MaterialInspectionCorrelationError(MaterialInspectionError):
    """Vision echoed values that do not match the immutable FMS request snapshot."""


class MaterialInspectionResultConflictError(MaterialInspectionError):
    """A terminal result attempted to overwrite different terminal evidence."""


class MaterialInspectionService:
    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def request_inspection(self, session: Session, delivery_item_id: int) -> IncomingMaterialQARequest:
        """Create one new immutable inspection cycle; caller commits before network I/O."""
        item = self.get_delivery_item_for_update(session, delivery_item_id)
        if item is None:
            raise MaterialInspectionError(f"Delivery item {delivery_item_id} not found.")
        part = session.execute(select(Part).where(Part.part_code == item.part_code)).scalar_one_or_none()
        if part is None:
            raise MaterialInspectionError(f"Part {item.part_code} not found.")
        if not part.vision_class:
            raise MaterialInspectionError(f"Part {item.part_code} has no vision_class mapping.")
        max_cycle = session.execute(select(func.max(MaterialInspection.inspection_cycle)).where(MaterialInspection.delivery_item_id == delivery_item_id)).scalar()
        next_cycle = (max_cycle or 0) + 1
        request_id = uuid.uuid4().hex
        inspection = MaterialInspection(inspection_request_id=request_id, delivery_item_id=delivery_item_id, inspection_cycle=next_cycle, status=MaterialInspectionStatus.REQUESTED, expected_part_code=item.part_code, expected_class_name=part.vision_class, expected_quantity=item.quantity)
        session.add(inspection)
        session.flush()
        logger.info("Incoming QA inspection created: inspection_request_id=%s delivery_item_id=%s inspection_cycle=%s", request_id, delivery_item_id, next_cycle)
        return self.request_from_inspection(inspection)

    @staticmethod
    def get_delivery_item_for_update(session: Session, delivery_item_id: int) -> JobMaterialDeliveryItem | None:
        """Lock one DeliveryItem before deciding whether to allocate a QA cycle."""
        return session.execute(select(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id == delivery_item_id).with_for_update()).scalar_one_or_none()

    @staticmethod
    def get_latest_inspection(session: Session, delivery_item_id: int, *, for_update: bool = False) -> MaterialInspection | None:
        """Return the latest item cycle; lock the DeliveryItem too when allocating."""
        statement = select(MaterialInspection).where(MaterialInspection.delivery_item_id == delivery_item_id).order_by(MaterialInspection.inspection_cycle.desc()).limit(1)
        if for_update:
            statement = statement.with_for_update()
        return session.execute(statement).scalar_one_or_none()

    @staticmethod
    def request_from_inspection(inspection: MaterialInspection) -> IncomingMaterialQARequest:
        """Build request wire data only from the persisted snapshot, never current catalog data."""
        return IncomingMaterialQARequest(ver="0.1", inspection_request_id=inspection.inspection_request_id, delivery_item_id=inspection.delivery_item_id, inspection_cycle=inspection.inspection_cycle, expected_part_code=inspection.expected_part_code, expected_class_name=inspection.expected_class_name, expected_quantity=inspection.expected_quantity)

    @staticmethod
    def is_release_allowed(inspection: MaterialInspection | None) -> bool:
        """Incoming QA releases only on durable completed PASS evidence.

        ``production_valid`` is retained as Vision Runtime authorization
        metadata. It is not an Incoming QA material-quality gate predicate.
        """
        return bool(
            inspection is not None
            and MaterialInspectionService._enum_value(inspection.status)
            == MaterialInspectionStatus.COMPLETED.value
            and MaterialInspectionService._enum_value(inspection.result)
            == MaterialInspectionResult.PASS.value
        )

    @staticmethod
    def _canonical_detections(result: IncomingMaterialQAResult) -> str:
        return json.dumps(result.model_dump(mode="json", include={"detections"}), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _enum_value(value: object | None) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value))

    @staticmethod
    def _normalized_timestamp(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _same_terminal_result(self, inspection: MaterialInspection, result: IncomingMaterialQAResult) -> bool:
        return (self._enum_value(inspection.status) == result.status and self._enum_value(inspection.result) == result.result and self._enum_value(inspection.failure_type) == result.failure_type and inspection.detected_quantity == result.detected_quantity and (inspection.detections_json or "") == self._canonical_detections(result) and inspection.frame_width == result.frame_width and inspection.frame_height == result.frame_height and self._enum_value(inspection.camera_source) == result.camera_source and inspection.frame_seq == result.frame_seq and self._normalized_timestamp(inspection.vision_timestamp) == self._normalized_timestamp(result.timestamp) and inspection.model_scope == result.model_scope and inspection.model_version == result.model_version and inspection.production_valid is result.production_valid)

    def apply_inspection_result(self, session: Session, result: IncomingMaterialQAResult) -> MaterialInspection:
        """Apply internal legacy/fake terminal evidence; never a v0.2 public authority."""
        inspections = list(session.scalars(
            select(MaterialInspection)
            .where(MaterialInspection.inspection_request_id == result.inspection_request_id)
            .with_for_update()
        ))
        if not inspections:
            raise MaterialInspectionNotFoundError(f"Inspection request {result.inspection_request_id} not found.")
        # v0.2 uses one request ID for all transaction items. Inspect every
        # matching row before selecting the legacy single-item authority so a
        # caller cannot bypass the v0.2 result runtime with a matching item.
        if any(inspection.incoming_qa_transaction_id is not None for inspection in inspections):
            raise MaterialInspectionCorrelationError(
                "Legacy Incoming QA result application cannot modify a v0.2 transaction-managed inspection."
            )
        if len(inspections) != 1:
            raise MaterialInspectionCorrelationError(
                "Legacy Incoming QA result must correlate to exactly one inspection."
            )
        inspection = inspections[0]
        if inspection.delivery_item_id != result.delivery_item_id:
            raise MaterialInspectionCorrelationError("Delivery item ID mismatch.")
        if inspection.inspection_cycle != result.inspection_cycle:
            raise MaterialInspectionCorrelationError("Inspection cycle mismatch.")
        if inspection.expected_part_code != result.expected_part_code:
            raise MaterialInspectionCorrelationError("Expected part code mismatch.")
        if inspection.expected_class_name != result.expected_class_name:
            raise MaterialInspectionCorrelationError("Expected class name mismatch.")
        if inspection.expected_quantity != result.expected_quantity:
            raise MaterialInspectionCorrelationError("Expected quantity mismatch.")
        if inspection.status in (MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR):
            if not self._same_terminal_result(inspection, result):
                raise MaterialInspectionResultConflictError("Conflicting terminal result already applied.")
            logger.info("Incoming QA terminal callback duplicated: inspection_request_id=%s delivery_item_id=%s inspection_cycle=%s", inspection.inspection_request_id, inspection.delivery_item_id, inspection.inspection_cycle)
            return inspection
        inspection.status = MaterialInspectionStatus(result.status)
        inspection.result = MaterialInspectionResult(result.result) if result.result is not None else None
        inspection.failure_type = MaterialInspectionFailureType(result.failure_type) if result.failure_type is not None else None
        inspection.detected_quantity = result.detected_quantity
        inspection.detections_json = self._canonical_detections(result)
        inspection.frame_width = result.frame_width
        inspection.frame_height = result.frame_height
        inspection.camera_source = result.camera_source
        inspection.frame_seq = result.frame_seq
        inspection.vision_timestamp = result.timestamp
        inspection.model_scope = result.model_scope
        inspection.model_version = result.model_version
        inspection.production_valid = result.production_valid
        inspection.failure_reason = "Vision reported incoming QA transaction ERROR" if result.status == "ERROR" else None
        inspection.completed_at = self._now()
        session.flush()
        logger.info("Incoming QA terminal callback applied: inspection_request_id=%s delivery_item_id=%s inspection_cycle=%s status=%s result=%s release_allowed=%s", inspection.inspection_request_id, inspection.delivery_item_id, inspection.inspection_cycle, inspection.status, inspection.result, self.is_release_allowed(inspection))
        return inspection

    def mark_running(self, session: Session, request_id: str) -> MaterialInspection:
        inspection = session.execute(select(MaterialInspection).where(MaterialInspection.inspection_request_id == request_id).with_for_update()).scalar_one_or_none()
        if inspection is None:
            raise MaterialInspectionNotFoundError(f"Inspection request {request_id} not found.")
        if inspection.status != MaterialInspectionStatus.REQUESTED:
            raise MaterialInspectionError(f"Cannot transition to RUNNING from {inspection.status}")
        inspection.status = MaterialInspectionStatus.RUNNING
        inspection.started_at = self._now()
        inspection.failure_reason = None
        session.flush()
        return inspection

    def record_send_failure(self, session: Session, request_id: str, failure_reason: str) -> MaterialInspection:
        """Keep a non-terminal request resendable after an unacknowledged send."""
        inspection = session.execute(select(MaterialInspection).where(MaterialInspection.inspection_request_id == request_id).with_for_update()).scalar_one_or_none()
        if inspection is None:
            raise MaterialInspectionNotFoundError(f"Inspection request {request_id} not found.")
        if inspection.status in (MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR):
            return inspection
        inspection.failure_reason = failure_reason
        session.flush()
        return inspection

    def mark_error(self, session: Session, request_id: str, failure_reason: str) -> MaterialInspection:
        inspection = session.execute(select(MaterialInspection).where(MaterialInspection.inspection_request_id == request_id).with_for_update()).scalar_one_or_none()
        if inspection is None:
            raise MaterialInspectionNotFoundError(f"Inspection request {request_id} not found.")
        self._mark_inspection_error(inspection, failure_reason=failure_reason)
        session.flush()
        return inspection

    def mark_transaction_inspections_error(
        self,
        session: Session,
        *,
        transaction_id: int,
        failure_reason: str,
    ) -> list[MaterialInspection]:
        """Fail-close every nonterminal v0.2 item in one locked transaction.

        A v0.2 request has one transaction and N item histories. Transport
        terminal failures have no item-level final Vision evidence, but they
        must not leave histories in REQUESTED/RUNNING and thereby occupy the
        global active-inspection predicate. The caller owns the surrounding
        commit so transaction and item terminalization are atomic.
        """
        inspections = list(
            session.scalars(
                select(MaterialInspection)
                .where(MaterialInspection.incoming_qa_transaction_id == transaction_id)
                .with_for_update()
            )
        )
        if not inspections:
            raise MaterialInspectionNotFoundError(
                f"Incoming QA transaction {transaction_id} has no persisted item inspections."
            )
        for inspection in inspections:
            self._mark_inspection_error(inspection, failure_reason=failure_reason)
        session.flush()
        return inspections

    def _mark_inspection_error(
        self,
        inspection: MaterialInspection,
        *,
        failure_reason: str,
    ) -> None:
        if inspection.status in (MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR):
            return
        inspection.status = MaterialInspectionStatus.ERROR
        inspection.result = None
        inspection.failure_type = None
        inspection.failure_reason = failure_reason
        inspection.completed_at = self._now()

    def is_delivery_item_released(self, session: Session, delivery_item_id: int) -> bool:
        latest = self.get_latest_inspection(session, delivery_item_id)
        return self.is_release_allowed(latest)
