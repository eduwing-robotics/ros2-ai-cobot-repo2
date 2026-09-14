"""FMS runtime orchestration for persisted Incoming Material QA transactions."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.incoming_material_qa_http_client import (
    IncomingMaterialQAAcknowledgement,
    IncomingMaterialQAHttpClient,
    IncomingMaterialQAHttpConflictError,
    IncomingMaterialQAHttpError,
    IncomingMaterialQAHttpTransportError,
)
from shared.models.factory import MaterialInspection, MaterialInspectionStatus
from shared.schemas.vision import IncomingMaterialQARequest
from shared.services.material_inspection_service import MaterialInspectionService

logger = logging.getLogger(__name__)


class IncomingMaterialQARuntime:
    """Persist first, send immutable snapshot second, then await callback separately."""

    def __init__(
        self,
        *,
        inspection_service: MaterialInspectionService | None = None,
        client: IncomingMaterialQAHttpClient | None = None,
    ) -> None:
        self._inspections = inspection_service or MaterialInspectionService()
        self._client = client or IncomingMaterialQAHttpClient()

    def create_transaction(self, session: Session, *, delivery_item_id: int) -> IncomingMaterialQARequest:
        """Create and commit REQUESTED before any network operation."""
        request = self._inspections.request_inspection(session, delivery_item_id)
        session.commit()
        logger.info(
            "Incoming QA request snapshot committed: inspection_request_id=%s delivery_item_id=%s inspection_cycle=%s",
            request.inspection_request_id,
            request.delivery_item_id,
            request.inspection_cycle,
        )
        return request

    def create_and_send(self, session: Session, *, delivery_item_id: int) -> IncomingMaterialQAAcknowledgement:
        request = self.create_transaction(session, delivery_item_id=delivery_item_id)
        return self.send_existing(session, inspection_request_id=request.inspection_request_id)

    def send_existing(self, session: Session, *, inspection_request_id: str) -> IncomingMaterialQAAcknowledgement:
        """Send/retry one existing transaction without allocating a new cycle or ID."""
        inspection = session.scalar(
            select(MaterialInspection).where(MaterialInspection.inspection_request_id == inspection_request_id)
        )
        if inspection is None:
            raise ValueError(f"Unknown inspection_request_id={inspection_request_id}.")
        if inspection.status in {MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR}:
            raise ValueError(f"Inspection {inspection_request_id} is terminal and cannot be re-sent.")

        request = self._inspections.request_from_inspection(inspection)
        try:
            acknowledgement = self._client.send_request(request)
        except IncomingMaterialQAHttpConflictError as exc:
            self._inspections.mark_error(session, inspection_request_id, str(exc))
            session.commit()
            raise
        except IncomingMaterialQAHttpTransportError as exc:
            # No HTTP acknowledgement is not a terminal physical inspection ERROR.
            # Keep this immutable snapshot resendable with the same ID/cycle.
            self._inspections.record_send_failure(session, inspection_request_id, str(exc))
            session.commit()
            raise
        except IncomingMaterialQAHttpError as exc:
            self._inspections.mark_error(session, inspection_request_id, str(exc))
            session.commit()
            raise

        # 202/200 only establish that Vision has the same transaction.  They do
        # not create RELEASE; callback terminal evidence remains authoritative.
        if inspection.status is MaterialInspectionStatus.REQUESTED:
            self._inspections.mark_running(session, inspection_request_id)
            session.commit()
        return acknowledgement
