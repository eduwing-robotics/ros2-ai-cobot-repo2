"""Incoming QA v0.2 UDP request/ACK/final-result runtime.

This module deliberately owns only one already-persisted
:class:`IncomingQATransaction` at a time.  It does not choose inspection modes,
calculate cycles, schedule HOUSE_B sequences, publish realtime events, or alter
production readiness.  It emits only a best-effort post-commit identity
notification for Unity observability; the existing latest-effective
MaterialInspection gate remains the authority after a valid terminal result has
been atomically stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    CameraSource,
    IncomingQATransaction,
    IncomingQATransactionStatus,
    MaterialInspection,
    MaterialInspectionFailureType,
    MaterialInspectionResult,
    MaterialInspectionStatus,
)
from shared.realtime.incoming_qa_events import get_incoming_qa_change_callback
from shared.realtime.production_events import get_production_change_callback
from shared.schemas.vision import (
    IncomingQAAckV02,
    IncomingQAMessageType,
    IncomingQARequestV02,
    IncomingQAResultV02,
    incoming_qa_result_item_evidence_json,
)
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService

logger = logging.getLogger(__name__)


class IncomingQAUdpRuntimeError(RuntimeError):
    """Base error for the isolated Incoming QA v0.2 UDP boundary."""


class IncomingQAUdpConfigurationError(IncomingQAUdpRuntimeError):
    """The intentionally explicit UDP runtime configuration is incomplete."""


class IncomingQACorrelationError(IncomingQAUdpRuntimeError):
    """An ACK or final result cannot safely be correlated to persisted intent."""


@dataclass(frozen=True, slots=True)
class IncomingQAUdpRuntimeConfig:
    """Explicit addresses and retry policy supplied by deployment or tests.

    No production host, port, timeout, or retry count is implied by this type.
    """

    vision_host: str
    vision_port: int
    result_host: str
    result_port: int
    ack_timeout_seconds: float
    max_retries: int

    def __post_init__(self) -> None:
        if not self.vision_host.strip() or not self.result_host.strip():
            raise IncomingQAUdpConfigurationError("Incoming QA UDP hosts must be non-empty.")
        # ``result_port=0`` is accepted only as a test-only ephemeral bind.
        # Deployment settings still require an explicit non-zero port.
        if not (1 <= self.vision_port <= 65535 and 0 <= self.result_port <= 65535):
            raise IncomingQAUdpConfigurationError("Incoming QA Vision port must be in 1..65535 and result port in 0..65535.")
        if self.ack_timeout_seconds <= 0:
            raise IncomingQAUdpConfigurationError("Incoming QA ACK timeout must be positive.")
        if self.max_retries < 0:
            raise IncomingQAUdpConfigurationError("Incoming QA max retries cannot be negative.")

    @classmethod
    def from_settings(cls, settings: Any) -> "IncomingQAUdpRuntimeConfig | None":
        """Return disabled for any missing field, never infer a port or policy."""

        values = {
            "vision_host": getattr(settings, "vision_incoming_qa_udp_host", None),
            "vision_port": getattr(settings, "vision_incoming_qa_udp_port", None),
            "result_host": getattr(settings, "fms_incoming_qa_result_udp_host", None),
            "result_port": getattr(settings, "fms_incoming_qa_result_udp_port", None),
            "ack_timeout_seconds": getattr(settings, "incoming_qa_udp_ack_timeout_seconds", None),
            "max_retries": getattr(settings, "incoming_qa_udp_max_retries", None),
        }
        missing = [key for key, value in values.items() if value is None or value == ""]
        if missing:
            logger.info(
                "Incoming QA v0.2 UDP runtime disabled: explicit configuration missing (%s)",
                ", ".join(missing),
            )
            return None
        return cls(**values)


def _canonical_result_snapshot(result: IncomingQAResultV02) -> str:
    """Canonical internal evidence; this is not a wire-protocol hash.

    Item ordering has no semantic authority, therefore repeated terminal payloads
    in a different datagram order still compare equal.
    """

    payload = result.model_dump(mode="json")
    payload["items"] = sorted(
        payload["items"], key=lambda item: (item["delivery_item_id"], item["slot_id"])
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class IncomingQAUdpDatagramProtocol(asyncio.DatagramProtocol):
    """Parse only Incoming QA v0.2 ACK/result packets and hand them to runtime."""

    def __init__(self, runtime: "IncomingQAUdpRuntime") -> None:
        self._runtime = runtime

    def datagram_received(self, data: bytes, addr: tuple[str, int] | tuple[Any, ...]) -> None:
        try:
            decoded = data.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Rejected Incoming QA UDP packet from %s: invalid UTF-8 JSON", addr)
            return
        if not isinstance(payload, dict):
            logger.warning("Rejected Incoming QA UDP packet from %s: JSON object required", addr)
            return

        message_type = payload.get("message_type")
        try:
            if message_type == IncomingQAMessageType.ACK.value:
                message = IncomingQAAckV02.model_validate(payload)
                self._runtime.schedule_ack(message)
            elif message_type == IncomingQAMessageType.RESULT.value:
                message = IncomingQAResultV02.model_validate(payload)
                self._runtime.schedule_result(message)
            else:
                logger.warning("Rejected Incoming QA UDP packet from %s: unexpected message_type", addr)
        except ValidationError as exc:
            logger.warning("Rejected Incoming QA UDP packet from %s: schema validation failed: %s", addr, exc)
        except Exception:  # pragma: no cover - defensive DatagramProtocol boundary
            logger.exception("Incoming QA UDP packet handoff failed from %s", addr)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Incoming QA UDP listener socket error: %s", exc)


class IncomingQAUdpRuntime:
    """FMS-owned UDP runtime for a persisted Incoming QA v0.2 transaction."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        config: IncomingQAUdpRuntimeConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._sender_transport: asyncio.DatagramTransport | None = None
        self._listener_transport: asyncio.DatagramTransport | None = None
        self._retry_tasks: dict[int, asyncio.Task[None]] = {}
        self._message_tasks: set[asyncio.Task[None]] = set()
        # ACK, RESULT, and retry handlers each own a complete DB transaction.
        # Serialize them for single-connection SQLite fixtures and retain every
        # worker so close() can drain it before the engine is torn down.
        self._db_lock = asyncio.Lock()
        self._db_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    @property
    def is_running(self) -> bool:
        return self._sender_transport is not None and self._listener_transport is not None

    @property
    def bound_port(self) -> int | None:
        if self._listener_transport is None:
            return None
        socket = self._listener_transport.get_extra_info("socket")
        return None if socket is None else int(socket.getsockname()[1])

    async def start(self) -> None:
        if self.is_running:
            return
        self._closing = False
        loop = asyncio.get_running_loop()
        listener, _ = await loop.create_datagram_endpoint(
            lambda: IncomingQAUdpDatagramProtocol(self),
            local_addr=(self._config.result_host, self._config.result_port),
        )
        try:
            # Vision returns ACKs directly to this persistent request socket's
            # source address. It must therefore use the same parser/handoff
            # protocol as the fixed final-result listener.
            sender, _ = await loop.create_datagram_endpoint(
                lambda: IncomingQAUdpDatagramProtocol(self),
                remote_addr=(self._config.vision_host, self._config.vision_port),
            )
        except Exception:
            listener.close()
            raise
        self._listener_transport = listener
        self._sender_transport = sender
        # A crash after durable SENT commit but before ACK must not force a
        # new request identity. Resume only those existing immutable requests.
        for transaction_id in await self._run_db(self._sent_transaction_ids):
            self._schedule_ack_timeout(transaction_id)
        logger.info(
            "Incoming QA v0.2 UDP runtime listening on %s:%s; Vision destination configured",
            self._config.result_host,
            self.bound_port,
        )

    async def close(self) -> None:
        self._closing = True
        if self._sender_transport is not None:
            self._sender_transport.close()
            self._sender_transport = None
        if self._listener_transport is not None:
            self._listener_transport.close()
            self._listener_transport = None
        retry_tasks = tuple(self._retry_tasks.values())
        self._retry_tasks.clear()
        for task in retry_tasks:
            task.cancel()
        message_tasks = tuple(self._message_tasks)
        if retry_tasks or message_tasks:
            await asyncio.gather(*retry_tasks, *message_tasks, return_exceptions=True)
        # Cancelling a task awaiting asyncio.to_thread() does not stop its
        # worker. Never return ownership to fixture teardown until it finishes.
        db_tasks = tuple(self._db_tasks)
        if db_tasks:
            await asyncio.gather(*db_tasks, return_exceptions=True)
        logger.info("Incoming QA v0.2 UDP runtime stopped")

    async def send_transaction(self, transaction_id: int) -> None:
        """Durably mark and send an existing request; never create history here."""

        self._require_started()
        payload = await self._run_db(self._prepare_send, transaction_id, False)
        self._send_payload(transaction_id, payload)
        self._schedule_ack_timeout(transaction_id)

    def schedule_ack(self, ack: IncomingQAAckV02) -> None:
        if self._closing:
            return
        self._track_message_task(asyncio.create_task(self._handle_ack_boundary(ack)))

    def schedule_result(self, result: IncomingQAResultV02) -> None:
        if self._closing:
            return
        self._track_message_task(asyncio.create_task(self._handle_result_boundary(result)))

    async def _handle_ack_boundary(self, ack: IncomingQAAckV02) -> None:
        try:
            await self.handle_ack(ack)
        except Exception:
            logger.exception("Incoming QA ACK handling failed request_id=%s", ack.inspection_request_id)

    async def _handle_result_boundary(self, result: IncomingQAResultV02) -> None:
        try:
            await self.handle_result(result)
        except IncomingQACorrelationError as exc:
            logger.warning("Rejected Incoming QA final result request_id=%s: %s", result.inspection_request_id, exc)
        except Exception:
            logger.exception("Incoming QA final-result handling failed request_id=%s", result.inspection_request_id)

    async def handle_ack(self, ack: IncomingQAAckV02) -> bool:
        """Apply a correlated ACK. Unknown/wrong-cycle packets mutate nothing."""

        applied, transaction_id = await self._run_db(self._apply_ack_sync, ack)
        if applied and transaction_id is not None:
            self._cancel_retry(transaction_id)
        return applied

    async def handle_result(self, result: IncomingQAResultV02) -> bool:
        """Validate all items then atomically apply a terminal result.

        A valid final result is accepted even when its UDP ACK was reordered or
        lost: terminal evidence is more authoritative than the transport ACK.
        """

        applied, transaction_id = await self._run_db(self._apply_result_sync, result)
        if applied and transaction_id is not None:
            self._cancel_retry(transaction_id)
        return applied

    def _require_started(self) -> None:
        if self._sender_transport is None:
            raise IncomingQAUdpRuntimeError("Incoming QA UDP runtime has not been started.")

    def _sent_transaction_ids(self) -> list[int]:
        with self._session_factory() as session:
            return list(session.scalars(
                select(IncomingQATransaction.transaction_id).where(
                    IncomingQATransaction.status == IncomingQATransactionStatus.SENT
                )
            ))

    def _prepare_send(self, transaction_id: int, retry: bool) -> bytes:
        with self._session_factory() as session:
            transaction = session.scalar(
                select(IncomingQATransaction)
                .where(IncomingQATransaction.transaction_id == transaction_id)
                .with_for_update()
            )
            if transaction is None:
                raise IncomingQACorrelationError(f"Incoming QA transaction {transaction_id} was not found.")
            if transaction.status in {
                IncomingQATransactionStatus.ACKED,
                IncomingQATransactionStatus.COMPLETED,
                IncomingQATransactionStatus.REJECTED,
                IncomingQATransactionStatus.ERROR,
            }:
                raise IncomingQAUdpRuntimeError(
                    f"Incoming QA transaction {transaction_id} is not sendable from {transaction.status.value}."
                )
            if retry:
                if transaction.status is not IncomingQATransactionStatus.SENT:
                    raise IncomingQAUdpRuntimeError("Only SENT transactions may be retried.")
                transaction.retry_count += 1
            else:
                if transaction.status is not IncomingQATransactionStatus.REQUESTED:
                    raise IncomingQAUdpRuntimeError("Initial send requires a REQUESTED transaction.")
                transaction.status = IncomingQATransactionStatus.SENT
                transaction.sent_at = datetime.now(timezone.utc)
            try:
                request = IncomingQARequestV02.model_validate_json(transaction.immutable_request_snapshot)
            except ValidationError as exc:  # immutable DB evidence is corrupted; do not emit UDP.
                transaction.status = IncomingQATransactionStatus.ERROR
                transaction.error_reason = "IMMUTABLE_REQUEST_SNAPSHOT_INVALID"
                MaterialInspectionService().mark_transaction_inspections_error(
                    session,
                    transaction_id=transaction.transaction_id,
                    failure_reason=transaction.error_reason,
                )
                session.commit()
                self._notify_after_commit(transaction.transaction_id)
                raise IncomingQAUdpRuntimeError("Persisted Incoming QA request snapshot is invalid.") from exc
            session.commit()
            self._notify_after_commit(transaction.transaction_id)
            return request.model_dump_json().encode("utf-8")

    def _send_payload(self, transaction_id: int, payload: bytes) -> None:
        self._require_started()
        assert self._sender_transport is not None
        try:
            self._sender_transport.sendto(payload)
        except Exception as exc:
            logger.exception("Incoming QA UDP request send failed for transaction=%s", transaction_id)
            self._mark_runtime_error(transaction_id, "UDP_SEND_FAILED")
            raise IncomingQAUdpRuntimeError("Incoming QA UDP request send failed.") from exc

    def _schedule_ack_timeout(self, transaction_id: int) -> None:
        self._cancel_retry(transaction_id)
        task = asyncio.create_task(self._retry_after_timeout(transaction_id))
        self._retry_tasks[transaction_id] = task

    async def _retry_after_timeout(self, transaction_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.ack_timeout_seconds)
                payload = await self._run_db(self._prepare_retry_or_error, transaction_id)
                if payload is None:
                    return
                self._send_payload(transaction_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Incoming QA UDP retry failed for transaction=%s", transaction_id)
            self._mark_runtime_error(transaction_id, "UDP_RETRY_RUNTIME_ERROR")
        finally:
            self._retry_tasks.pop(transaction_id, None)

    def _prepare_retry_or_error(self, transaction_id: int) -> bytes | None:
        with self._session_factory() as session:
            transaction = session.scalar(
                select(IncomingQATransaction)
                .where(IncomingQATransaction.transaction_id == transaction_id)
                .with_for_update()
            )
            if transaction is None or transaction.status is not IncomingQATransactionStatus.SENT:
                return None
            if transaction.retry_count >= self._config.max_retries:
                transaction.status = IncomingQATransactionStatus.ERROR
                transaction.error_reason = "ACK_TIMEOUT_MAX_RETRIES"
                MaterialInspectionService().mark_transaction_inspections_error(
                    session,
                    transaction_id=transaction.transaction_id,
                    failure_reason=transaction.error_reason,
                )
                session.commit()
                self._notify_after_commit(transaction.transaction_id)
                logger.warning("Incoming QA transaction=%s exhausted ACK retries", transaction_id)
                return None
            transaction.retry_count += 1
            request = IncomingQARequestV02.model_validate_json(transaction.immutable_request_snapshot)
            session.commit()
            # Retry count is Unity-visible diagnostic state even though the
            # lifecycle remains SENT. Notify only after the durable increment.
            self._notify_after_commit(transaction.transaction_id)
            return request.model_dump_json().encode("utf-8")

    def _apply_ack_sync(self, ack: IncomingQAAckV02) -> tuple[bool, int | None]:
        with self._session_factory() as session:
            transaction = session.scalar(
                select(IncomingQATransaction)
                .where(IncomingQATransaction.inspection_request_id == ack.inspection_request_id)
                .with_for_update()
            )
            if transaction is None or transaction.inspection_cycle != ack.inspection_cycle:
                logger.warning("Ignored uncorrelated Incoming QA ACK request_id=%s", ack.inspection_request_id)
                return False, None
            # ERROR remains terminal for ACK handling. COMPLETED is different:
            # a valid final result may be persisted before its independently
            # scheduled UDP ACK task. Preserve that observed ACK as diagnostics,
            # but never roll the completed lifecycle state backward.
            if transaction.status is IncomingQATransactionStatus.ERROR:
                return False, None
            transaction.ack_accepted = ack.accepted
            transaction.ack_duplicate = ack.duplicate
            transaction.ack_reason_code = None if ack.reason_code is None else ack.reason_code.value
            transaction.acked_at = datetime.now(timezone.utc)
            if transaction.status is IncomingQATransactionStatus.COMPLETED:
                session.commit()
                self._notify_after_commit(transaction.transaction_id)
                return True, transaction.transaction_id
            if ack.accepted:
                transaction.status = IncomingQATransactionStatus.ACKED
                transaction.error_reason = None
            else:
                transaction.status = IncomingQATransactionStatus.REJECTED
                transaction.error_reason = None
                item_failure_reason = (
                    "ACK_REJECTED"
                    if ack.reason_code is None
                    else f"ACK_REJECTED:{ack.reason_code.value}"
                )
                MaterialInspectionService().mark_transaction_inspections_error(
                    session,
                    transaction_id=transaction.transaction_id,
                    failure_reason=item_failure_reason,
                )
            session.commit()
            self._notify_after_commit(transaction.transaction_id)
            return True, transaction.transaction_id

    def _apply_result_sync(self, result: IncomingQAResultV02) -> tuple[bool, int | None]:
        result_snapshot = _canonical_result_snapshot(result)
        with self._session_factory() as session:
            transaction = session.scalar(
                select(IncomingQATransaction)
                .where(IncomingQATransaction.inspection_request_id == result.inspection_request_id)
                .with_for_update()
            )
            if transaction is None:
                logger.warning("Ignored uncorrelated Incoming QA result request_id=%s", result.inspection_request_id)
                return False, None
            if (
                transaction.inspection_cycle != result.inspection_cycle
                or transaction.inspection_mode != result.inspection_mode.value
            ):
                logger.warning("Ignored Incoming QA result with immutable transaction mismatch request_id=%s", result.inspection_request_id)
                return False, None
            if transaction.status is IncomingQATransactionStatus.COMPLETED:
                if transaction.result_snapshot_json == result_snapshot:
                    return True, transaction.transaction_id
                logger.warning("Ignored conflicting terminal Incoming QA result request_id=%s", result.inspection_request_id)
                return False, None
            if transaction.status in {IncomingQATransactionStatus.REJECTED, IncomingQATransactionStatus.ERROR}:
                return False, None

            request = IncomingQARequestV02.model_validate_json(transaction.immutable_request_snapshot)
            expected = {item.delivery_item_id: item for item in request.items}
            actual = {item.delivery_item_id: item for item in result.items}
            if len(actual) != len(result.items) or set(actual) != set(expected):
                raise IncomingQACorrelationError("Final result item identities do not exactly match immutable request.")
            for delivery_item_id, expected_item in expected.items():
                item = actual[delivery_item_id]
                if (
                    item.slot_id != expected_item.slot_id
                    or item.expected_part_code != expected_item.expected_part_code
                    or item.expected_class_name != expected_item.expected_class_name
                    or item.expected_quantity != expected_item.expected_quantity
                ):
                    raise IncomingQACorrelationError(
                        f"Final result immutable item context mismatch delivery_item_id={delivery_item_id}."
                    )

            inspections = session.scalars(
                select(MaterialInspection)
                .where(MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id)
                .with_for_update()
            ).all()
            inspections_by_item = {inspection.delivery_item_id: inspection for inspection in inspections}
            if len(inspections_by_item) != len(expected) or set(inspections_by_item) != set(expected):
                raise IncomingQACorrelationError("Persisted transaction inspection set does not match immutable request.")

            # Every validation above occurs before the first state mutation.
            completed_at = datetime.now(timezone.utc)
            transaction.status = IncomingQATransactionStatus.COMPLETED
            transaction.overall_result = MaterialInspectionResult(result.result)
            transaction.production_valid = result.production_valid
            transaction.result_snapshot_json = result_snapshot
            transaction.camera_source = result.camera_source
            transaction.vision_timestamp = result.timestamp
            transaction.model_scope = result.model_scope
            transaction.model_version = result.model_version
            transaction.completed_at = completed_at
            transaction.error_reason = None

            try:
                camera_source = CameraSource(result.camera_source)
            except ValueError:
                camera_source = None
            for delivery_item_id, item in actual.items():
                inspection = inspections_by_item[delivery_item_id]
                inspection.status = MaterialInspectionStatus.COMPLETED
                inspection.result = MaterialInspectionResult(item.result)
                inspection.failure_type = (
                    None if item.failure_type is None else MaterialInspectionFailureType(item.failure_type)
                )
                inspection.predicted_class_name = item.predicted_class_name
                inspection.material_confidence = item.material_confidence
                inspection.detected_quantity = item.detected_quantity
                inspection.result_detail_json = json.dumps(
                    incoming_qa_result_item_evidence_json(item),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                inspection.camera_source = camera_source
                inspection.vision_timestamp = result.timestamp
                inspection.model_scope = result.model_scope
                inspection.model_version = result.model_version
                # Result-level validity is a request aggregate. Item validity is
                # derived server-side from the frozen v0.2 item result contract.
                inspection.production_valid = item.result == MaterialInspectionResult.PASS.value
                inspection.completed_at = completed_at
            session.commit()
            self._notify_after_commit(transaction.transaction_id)
            # The inspection commit is authoritative.  Only after it succeeds
            # may a fresh readiness read decide whether this result released
            # the production gate.
            if IncomingQAOrchestrationService(session).is_preproduction_ready(
                job_id=transaction.production_job_id
            ):
                self._notify_production_after_commit(
                    transaction.production_job_id,
                    "incoming_qa_gate_released",
                )
            return True, transaction.transaction_id

    def _mark_runtime_error(self, transaction_id: int, reason: str) -> None:
        try:
            with self._session_factory() as session:
                transaction = session.scalar(
                    select(IncomingQATransaction)
                    .where(IncomingQATransaction.transaction_id == transaction_id)
                    .with_for_update()
                )
                if transaction is not None and transaction.status is IncomingQATransactionStatus.SENT:
                    transaction.status = IncomingQATransactionStatus.ERROR
                    transaction.error_reason = reason
                    MaterialInspectionService().mark_transaction_inspections_error(
                        session,
                        transaction_id=transaction.transaction_id,
                        failure_reason=reason,
                    )
                    session.commit()
                    self._notify_after_commit(transaction.transaction_id)
        except Exception:  # pragma: no cover - cannot safely do more at an error boundary
            logger.exception("Failed to persist Incoming QA UDP runtime error transaction=%s", transaction_id)

    @staticmethod
    def _notify_after_commit(transaction_id: int) -> None:
        callback = get_incoming_qa_change_callback()
        if callback is None:
            return
        try:
            callback(transaction_id)
        except Exception:  # notification must never alter committed QA state
            logger.exception("Incoming QA post-commit notification failed transaction=%s", transaction_id)

    @staticmethod
    def _notify_production_after_commit(job_id: int, reason: str) -> None:
        callback = get_production_change_callback()
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:  # notification must never alter committed QA state
            logger.exception("Incoming QA production post-commit notification failed job_id=%s", job_id)

    def _cancel_retry(self, transaction_id: int) -> None:
        task = self._retry_tasks.pop(transaction_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _track_message_task(self, task: asyncio.Task[None]) -> None:
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)

    async def _run_db(self, operation: Callable[..., Any], *args: Any) -> Any:
        async with self._db_lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            self._db_tasks.add(task)
            task.add_done_callback(self._db_tasks.discard)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # shield keeps the worker alive; keep the serialization lock
                # until its transaction has really finished.
                await asyncio.gather(task, return_exceptions=True)
                raise
