"""Read-only, bounded Incoming QA v0.2 projections for Unity."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from shared.models.factory import IncomingQATransaction, IncomingQATransactionStatus
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_v02_monitoring_service import IncomingQAV02MonitoringService


_ACTIVE = frozenset({
    IncomingQATransactionStatus.REQUESTED,
    IncomingQATransactionStatus.SENT,
    IncomingQATransactionStatus.ACKED,
})
_TERMINAL = frozenset({
    IncomingQATransactionStatus.COMPLETED,
    IncomingQATransactionStatus.REJECTED,
    IncomingQATransactionStatus.ERROR,
})


class IncomingQAJobGateState(StrEnum):
    RELEASED = "RELEASED"
    NOT_RELEASED = "NOT_RELEASED"


class UnityIncomingQAProjectionService:
    """Project persisted v0.2 state without exposing raw Vision evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_transaction_status(self, *, transaction_id: int) -> dict[str, Any] | None:
        transaction = self._session.get(IncomingQATransaction, transaction_id)
        if transaction is None:
            return None
        monitor = IncomingQAV02MonitoringService(self._session).get_job_monitor(
            job_id=transaction.production_job_id
        )
        projected = next(
            (item for item in monitor.transactions if item.transaction_id == transaction_id), None
        )
        if projected is None:  # pragma: no cover - transaction is a monitor source row
            return None
        return {
            "job_id": transaction.production_job_id,
            "job_gate_state": self._job_gate_state(job_id=transaction.production_job_id),
            "transaction": self._transaction(projected),
            "items": [self._item(item) for item in projected.items],
        }

    def get_job_snapshot(self, *, job_id: int) -> dict[str, Any] | None:
        monitor = IncomingQAV02MonitoringService(self._session).get_job_monitor(job_id=job_id)
        transactions = list(monitor.transactions)
        if not transactions:
            return None
        selected = self._snapshot_transactions(transactions)
        return {
            "job_id": job_id,
            "job_gate_state": self._job_gate_state(job_id=job_id),
            "gate": {
                "status": monitor.gate.status,
                "total_expected_items": monitor.gate.total_expected_items,
                "released_items": monitor.gate.released_items,
            },
            "transactions": [self._transaction(transaction) for transaction in selected],
            "items": self._latest_items(transactions),
        }

    def get_snapshots(self, *, job_ids: Iterable[int]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for job_id in sorted({value for value in job_ids if isinstance(value, int) and value > 0}):
            snapshot = self.get_job_snapshot(job_id=job_id)
            if snapshot is not None:
                result.append(snapshot)
        return result

    def _job_gate_state(self, *, job_id: int) -> str:
        readiness = IncomingQAOrchestrationService(self._session).preproduction_readiness(
            job_id=job_id
        )
        return (
            IncomingQAJobGateState.RELEASED.value
            if readiness.ready
            else IncomingQAJobGateState.NOT_RELEASED.value
        )

    @staticmethod
    def _snapshot_transactions(transactions: list[Any]) -> list[Any]:
        active = [transaction for transaction in transactions if transaction.status in _ACTIVE]
        latest_terminal: dict[str, Any] = {}
        for transaction in transactions:
            if transaction.status in _TERMINAL:
                latest_terminal[transaction.inspection_mode] = transaction
        selected_ids = {transaction.transaction_id for transaction in active}
        selected_ids.update(transaction.transaction_id for transaction in latest_terminal.values())
        return [transaction for transaction in transactions if transaction.transaction_id in selected_ids]

    @classmethod
    def _latest_items(cls, transactions: list[Any]) -> list[dict[str, Any]]:
        latest: dict[int, Any] = {}
        for transaction in transactions:
            for item in transaction.items:
                current = latest.get(item.delivery_item_id)
                if current is None or item.inspection_cycle > current.inspection_cycle:
                    latest[item.delivery_item_id] = item
        return [cls._item(latest[item_id]) for item_id in sorted(latest)]

    @staticmethod
    def _transaction(transaction: Any) -> dict[str, Any]:
        return {
            "transaction_id": transaction.transaction_id,
            "request_id": transaction.inspection_request_id,
            "mode": transaction.inspection_mode,
            "cycle": transaction.inspection_cycle,
            "status": transaction.status.value,
            "retry_count": transaction.retry_count,
            "overall_result": (
                transaction.overall_result.value if transaction.overall_result is not None else None
            ),
            "production_valid": transaction.production_valid,
            "error_reason": transaction.error_reason,
        }

    @staticmethod
    def _item(item: Any) -> dict[str, Any]:
        return {
            "delivery_item_id": item.delivery_item_id,
            "slot_id": item.slot_id,
            "expected_part_code": item.expected_part_code,
            "status": item.status.value if item.status is not None else None,
            "result": item.result.value if item.result is not None else None,
            "failure_type": item.failure_type.value if item.failure_type is not None else None,
            "failure_reason": item.failure_reason,
            "defects": list(item.defects),
        }
