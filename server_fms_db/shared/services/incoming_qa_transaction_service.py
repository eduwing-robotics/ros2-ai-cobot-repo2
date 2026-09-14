"""Persistence-only foundation for Vision Incoming QA v0.2 transactions.

No UDP socket, retry timer, ACK runtime, result listener, Worker scheduling, or
Unity/Redis publication is implemented here.  This service only establishes the
durable 1:N request-to-item relationship that those later boundaries will use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionStatus,
    Part,
    ProductionJob,
)
from shared.schemas.vision import IncomingQARequestV02


class IncomingQATransactionError(RuntimeError):
    """Base v0.2 transaction-persistence error."""


class IncomingQATransactionCorrelationError(IncomingQATransactionError):
    """Request items do not belong to the stated job or immutable snapshot."""


class IncomingQATransactionConflictError(IncomingQATransactionError):
    """A reused request ID has different immutable context."""


@dataclass(frozen=True, slots=True)
class IncomingQATransactionCreation:
    transaction: IncomingQATransaction
    created: bool


def canonical_request_snapshot(request: IncomingQARequestV02) -> str:
    """Canonicalize immutable context without making a hash protocol authority.

    Item ordering is normalized by durable delivery-item identity, then slot, so
    UDP retransmission need not preserve array ordering to be idempotent.
    """
    items = sorted(
        (
            {
                "slot_id": item.slot_id,
                "delivery_item_id": item.delivery_item_id,
                "expected_part_code": item.expected_part_code,
                "expected_class_name": item.expected_class_name,
                "expected_quantity": item.expected_quantity,
            }
            for item in request.items
        ),
        key=lambda item: (item["delivery_item_id"], item["slot_id"]),
    )
    return json.dumps(
        {
            "ver": request.ver,
            "message_type": request.message_type.value,
            "inspection_request_id": request.inspection_request_id,
            "inspection_cycle": request.inspection_cycle,
            "inspection_mode": request.inspection_mode.value,
            "items": items,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class IncomingQATransactionService:
    """Create or reuse a durable v0.2 request and its item inspection histories."""

    def create_or_get(
        self,
        session: Session,
        *,
        production_job_id: int,
        request: IncomingQARequestV02,
    ) -> IncomingQATransactionCreation:
        """Persist one immutable transaction; caller owns commit/rollback.

        ``inspection_cycle`` is retained exactly at transaction scope.  Each
        MaterialInspection continues to allocate its own per-delivery-item history
        cycle, avoiding an unapproved global-vs-per-item cycle policy decision.
        """
        # One global lock serializes item-cycle allocation across FMS/API
        # processes.  The lock is held only for durable DB work, never UDP I/O.
        from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService

        IncomingQAOrchestrationService(session).acquire_global_transaction_guard()
        if session.get(ProductionJob, production_job_id) is None:
            raise IncomingQATransactionCorrelationError(
                f"Production job {production_job_id} not found."
            )
        snapshot = canonical_request_snapshot(request)
        existing = session.scalar(
            select(IncomingQATransaction)
            .where(IncomingQATransaction.inspection_request_id == request.inspection_request_id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.production_job_id != production_job_id
                or existing.inspection_mode != request.inspection_mode.value
                or existing.inspection_cycle != request.inspection_cycle
                or existing.immutable_request_snapshot != snapshot
            ):
                raise IncomingQATransactionConflictError(
                    "inspection_request_id already exists with immutable context conflict."
                )
            return IncomingQATransactionCreation(transaction=existing, created=False)

        items = self._locked_items_for_request(
            session,
            production_job_id=production_job_id,
            request=request,
        )
        transaction = IncomingQATransaction(
            inspection_request_id=request.inspection_request_id,
            production_job_id=production_job_id,
            inspection_mode=request.inspection_mode.value,
            inspection_cycle=request.inspection_cycle,
            status=IncomingQATransactionStatus.REQUESTED,
            immutable_request_snapshot=snapshot,
            retry_count=0,
        )
        session.add(transaction)
        session.flush()

        # The v0.2 wire cycle is an item-history cycle.  One transaction has
        # one cycle field, so the planner may group only items whose next cycle
        # is identical.  Rejecting a mismatch here is the final durable guard.
        for request_item in request.items:
            item, part = items[request_item.delivery_item_id]
            max_cycle = session.scalar(
                select(func.max(MaterialInspection.inspection_cycle)).where(
                    MaterialInspection.delivery_item_id == item.delivery_item_id
                )
            )
            next_cycle = (max_cycle or 0) + 1
            if next_cycle != request.inspection_cycle:
                raise IncomingQATransactionCorrelationError(
                    "Incoming QA transaction cycle must equal every item's next inspection cycle; "
                    f"delivery_item_id={item.delivery_item_id} next={next_cycle} "
                    f"request={request.inspection_cycle}."
                )
            session.add(
                MaterialInspection(
                    inspection_request_id=request.inspection_request_id,
                    incoming_qa_transaction_id=transaction.transaction_id,
                    delivery_item_id=item.delivery_item_id,
                    inspection_cycle=request.inspection_cycle,
                    status=MaterialInspectionStatus.REQUESTED,
                    expected_part_code=request_item.expected_part_code,
                    expected_class_name=request_item.expected_class_name,
                    expected_quantity=request_item.expected_quantity,
                )
            )
        session.flush()
        return IncomingQATransactionCreation(transaction=transaction, created=True)

    @staticmethod
    def _locked_items_for_request(
        session: Session,
        *,
        production_job_id: int,
        request: IncomingQARequestV02,
    ) -> dict[int, tuple[JobMaterialDeliveryItem, Part]]:
        requested = {item.delivery_item_id: item for item in request.items}
        rows = list(
            session.execute(
                select(JobMaterialDeliveryItem, Part)
                .join(
                    JobMaterialDelivery,
                    JobMaterialDelivery.job_delivery_id == JobMaterialDeliveryItem.job_delivery_id,
                )
                .join(Part, Part.part_code == JobMaterialDeliveryItem.part_code)
                .where(
                    JobMaterialDelivery.production_job_id == production_job_id,
                    JobMaterialDeliveryItem.delivery_item_id.in_(requested),
                )
                .with_for_update()
            )
        )
        if len(rows) != len(requested):
            raise IncomingQATransactionCorrelationError(
                "Every v0.2 request delivery_item_id must belong to the stated production job."
            )
        resolved: dict[int, tuple[JobMaterialDeliveryItem, Part]] = {}
        for item, part in rows:
            request_item = requested[item.delivery_item_id]
            if (
                request_item.expected_part_code != item.part_code
                or request_item.expected_quantity != item.quantity
                or request_item.expected_class_name != part.vision_class
            ):
                raise IncomingQATransactionCorrelationError(
                    f"Immutable context does not match delivery_item_id={item.delivery_item_id}."
                )
            resolved[item.delivery_item_id] = (item, part)
        return resolved
