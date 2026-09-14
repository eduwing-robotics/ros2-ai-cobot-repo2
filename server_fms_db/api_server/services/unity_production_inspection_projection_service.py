"""Canonical latest-cycle ProductionInspection projections for Unity."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from shared.models.factory import (
    ProductionInspection,
    ProductionInspectionResult,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    ProductionInspectionViewRequest,
)


class ProductionInspectionGateState(StrEnum):
    RELEASED = "RELEASED"
    NOT_RELEASED = "NOT_RELEASED"


_VIEW_CODES = (
    ("VIEW_TOP", "TOP"),
    ("VIEW_LEFT", "LEFT"),
    ("VIEW_RIGHT", "RIGHT"),
    ("VIEW_FRONT", "FRONT"),
    ("VIEW_BEHIND", "BEHIND"),
)


class UnityProductionInspectionProjectionService:
    """Read persisted authority only; no UDP or raw Vision payload is exposed."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_inspection_status(self, *, inspection_id: int) -> dict[str, Any] | None:
        inspected = self._session.get(ProductionInspection, inspection_id)
        if inspected is None:
            return None
        latest = self._latest(inspected.production_job_id, inspected.inspection_type)
        return None if latest is None else self._status(latest)

    def get_snapshots(self, *, job_ids: Iterable[int]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for job_id in sorted({value for value in job_ids if isinstance(value, int) and value > 0}):
            rows = list(self._session.scalars(
                select(ProductionInspection)
                .options(selectinload(ProductionInspection.results), selectinload(ProductionInspection.view_requests))
                .execution_options(populate_existing=True)
                .where(ProductionInspection.production_job_id == job_id)
                .order_by(ProductionInspection.inspection_type, ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            ))
            latest_by_type: dict[str, ProductionInspection] = {}
            for row in rows:
                latest_by_type.setdefault(row.inspection_type.value, row)
            result.extend(self._status(row) for _, row in sorted(latest_by_type.items()))
        return result

    def _latest(self, job_id: int, inspection_type: Any) -> ProductionInspection | None:
        return self._session.scalar(
            select(ProductionInspection)
            .options(selectinload(ProductionInspection.results), selectinload(ProductionInspection.view_requests))
            .execution_options(populate_existing=True)
            .where(
                ProductionInspection.production_job_id == job_id,
                ProductionInspection.inspection_type == inspection_type,
            )
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )

    def _status(self, inspection: ProductionInspection) -> dict[str, Any]:
        return {
            "job_id": inspection.production_job_id,
            "inspection_type": inspection.inspection_type.value,
            "inspection": {
                "inspection_id": inspection.inspection_id,
                "inspection_request_id": inspection.inspection_request_id,
                "inspection_cycle": inspection.inspection_cycle,
                "status": inspection.status.value,
                "result": inspection.result.value if inspection.result is not None else None,
                "vision_production_valid": inspection.vision_production_valid,
                "production_valid": inspection.production_valid,
                "gate_state": self._gate_state(inspection),
                "transport": self._transport(inspection),
                "current_view": self._current_view(inspection.view_requests),
                "views": self._views(inspection.results, inspection.view_requests),
                "runtime_profile": inspection.runtime_profile,
                "runtime_versions": self._runtime_versions(inspection.runtime_versions_json),
                "requested_at": _timestamp(inspection.requested_at),
                "started_at": _timestamp(inspection.started_at),
                "completed_at": _timestamp(inspection.completed_at),
            },
        }

    @staticmethod
    def _gate_state(inspection: ProductionInspection) -> str:
        return (
            ProductionInspectionGateState.RELEASED.value
            if (
                inspection.status is ProductionInspectionStatus.COMPLETED
                and inspection.result is ProductionInspectionResultCode.PASS
            )
            else ProductionInspectionGateState.NOT_RELEASED.value
        )

    @staticmethod
    def _transport(inspection: ProductionInspection) -> dict[str, Any]:
        active = next((row for row in reversed(inspection.view_requests) if row.status in {"REQUESTED", "SENT", "ACKED"}), None)
        if active is None:
            return {"request_sent_at": _timestamp(inspection.wire_sent_at), "acked": inspection.wire_acked_at is not None, "acked_at": _timestamp(inspection.wire_acked_at), "retry_count": inspection.wire_retry_count, "wire_error_code": inspection.wire_error_code}
        return {"request_sent_at": _timestamp(active.sent_at), "acked": active.acked_at is not None, "acked_at": _timestamp(active.acked_at), "retry_count": active.retry_count, "wire_error_code": active.error_code}

    @staticmethod
    def _current_view(rows: list[ProductionInspectionViewRequest]) -> str | None:
        active = next((row for row in reversed(rows) if row.status in {"REQUESTED", "SENT", "ACKED"}), None)
        return None if active is None else active.view_name

    @staticmethod
    def _views(rows: list[ProductionInspectionResult], requests: list[ProductionInspectionViewRequest]) -> list[dict[str, str | None]]:
        if requests:
            latest: dict[str, ProductionInspectionViewRequest] = {}
            for row in requests:
                latest[row.view_name] = row
            output: list[dict[str, str | None]] = []
            for _, name in _VIEW_CODES:
                row = latest.get(name)
                if row is None:
                    output.append({"view_name": name, "status": "PENDING", "result": None})
                elif row.status == "COMPLETED":
                    output.append({"view_name": name, "status": row.result.value if row.result else "COMPLETED", "result": row.result.value if row.result else None})
                else:
                    output.append({"view_name": name, "status": "IN_PROGRESS", "result": None})
            return output
        by_code = {row.item_code: row for row in rows}
        return [{"view_name": name, "result": row.result.value, "reason_code": row.notes if row.result is ProductionInspectionResultCode.NOT_EVALUATED else None} for code, name in _VIEW_CODES if (row := by_code.get(code)) is not None]

    @staticmethod
    def _runtime_versions(value: str | None) -> dict[str, str] | None:
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()):
            return None
        return parsed


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
