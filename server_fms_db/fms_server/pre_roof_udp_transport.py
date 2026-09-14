"""FMS-owned PRE_ROOF Vision UDP v0.2 per-view runtime.

The server chooses the next view from durable state.  UDP is only a wakeup:
request identity, retry evidence, and result ordering are all persisted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import socket
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import ProductionInspection, ProductionInspectionStatus, ProductionInspectionType, ProductionInspectionViewRequest
from shared.schemas.pre_roof_vision import (
    PreRoofViewInspectionAckV02, PreRoofViewInspectionResultV02,
    PreRoofViewMessageType,
)
from shared.realtime.production_inspection_events import get_production_inspection_change_callback
from shared.services.production_completion_service import PreRoofVisionApplyDisposition, ProductionCompletionService

logger = logging.getLogger(__name__)


class PreRoofUdpRuntimeError(RuntimeError):
    pass


class PreRoofUdpConfigurationError(PreRoofUdpRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreRoofUdpRuntimeConfig:
    vision_host: str
    vision_port: int
    result_host: str
    result_port: int
    ack_timeout_seconds: float
    max_retries: int
    result_bind_host: str = "0.0.0.0"

    def __post_init__(self) -> None:
        if not self.vision_host.strip() or not self.result_host.strip() or not self.result_bind_host.strip():
            raise PreRoofUdpConfigurationError("PRE_ROOF UDP hosts must be non-empty.")
        if not 1 <= self.vision_port <= 65535 or not 0 <= self.result_port <= 65535:
            raise PreRoofUdpConfigurationError("PRE_ROOF UDP ports are invalid.")
        if self.ack_timeout_seconds <= 0 or self.max_retries < 0:
            raise PreRoofUdpConfigurationError("PRE_ROOF retry configuration is invalid.")

    @classmethod
    def from_settings(cls, settings: Any) -> "PreRoofUdpRuntimeConfig | None":
        values = {
            "vision_host": getattr(settings, "vision_pre_roof_udp_host", None),
            "vision_port": getattr(settings, "vision_pre_roof_udp_port", None),
            "result_host": getattr(settings, "fms_pre_roof_result_udp_host", None),
            "result_port": getattr(settings, "fms_pre_roof_result_udp_port", None),
            "ack_timeout_seconds": getattr(settings, "pre_roof_udp_ack_timeout_seconds", None),
            "max_retries": getattr(settings, "pre_roof_udp_max_retries", None),
        }
        if any(value is None or value == "" for value in values.values()):
            logger.info("PRE_ROOF UDP runtime disabled: explicit configuration incomplete")
            return None
        return cls(**values, result_bind_host=getattr(settings, "fms_pre_roof_result_udp_bind_host", "0.0.0.0") or "0.0.0.0")


def pre_roof_result_semantic_digest(result: PreRoofViewInspectionResultV02) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("timestamp", None)
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class PreRoofUdpDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, runtime: "PreRoofUdpRuntime") -> None:
        self._runtime = runtime

    def datagram_received(self, data: bytes, addr: tuple[str, int] | tuple[Any, ...]) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Rejected PRE_ROOF UDP packet from %s: invalid UTF-8 JSON", addr)
            return
        if not isinstance(payload, dict) or not self._runtime.is_expected_vision_source(addr):
            logger.warning("Rejected PRE_ROOF UDP packet from unexpected/invalid source=%s", addr)
            return
        try:
            if payload.get("message_type") == PreRoofViewMessageType.ACK.value:
                self._runtime.schedule_ack(PreRoofViewInspectionAckV02.model_validate(payload))
            elif payload.get("message_type") == PreRoofViewMessageType.RESULT.value:
                self._runtime.schedule_result(PreRoofViewInspectionResultV02.model_validate(payload))
            else:
                # v0.1 bundled packets are intentionally unsupported, never
                # coerced into v0.2 state.
                logger.warning("Rejected unsupported PRE_ROOF message_type=%r", payload.get("message_type"))
        except ValidationError as exc:
            logger.warning("Rejected PRE_ROOF v0.2 packet from %s: schema validation failed: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        logger.warning("PRE_ROOF UDP socket error: %s", exc)


class PreRoofUdpRuntime:
    def __init__(self, *, session_factory: Callable[[], Session], config: PreRoofUdpRuntimeConfig) -> None:
        self._session_factory = session_factory
        self._config = config
        self._sender_transport: asyncio.DatagramTransport | None = None
        self._listener_transport: asyncio.DatagramTransport | None = None
        self._retry_tasks: dict[int, asyncio.Task[None]] = {}
        self._message_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_running(self) -> bool:
        return self._sender_transport is not None and self._listener_transport is not None

    @property
    def bound_port(self) -> int | None:
        if self._listener_transport is None:
            return None
        sock = self._listener_transport.get_extra_info("socket")
        return None if sock is None else int(sock.getsockname()[1])

    async def start(self) -> None:
        if self.is_running:
            return
        loop = asyncio.get_running_loop()
        listener, _ = await loop.create_datagram_endpoint(lambda: PreRoofUdpDatagramProtocol(self), local_addr=(self._config.result_bind_host, self._config.result_port))
        try:
            sender, _ = await loop.create_datagram_endpoint(lambda: PreRoofUdpDatagramProtocol(self), remote_addr=(self._config.vision_host, self._config.vision_port))
        except Exception:
            listener.close()
            raise
        self._listener_transport, self._sender_transport = listener, sender
        for request_id in await asyncio.to_thread(self._sent_unacked_view_request_ids):
            self._schedule_ack_timeout(request_id)

    async def close(self) -> None:
        tasks = tuple(self._retry_tasks.values()) + tuple(self._message_tasks)
        self._retry_tasks.clear()
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for name in ("_sender_transport", "_listener_transport"):
            transport = getattr(self, name)
            if transport is not None:
                transport.close(); setattr(self, name, None)

    async def send_inspection(self, inspection_id: int) -> None:
        self._require_started()
        request_id, payload = await asyncio.to_thread(self._prepare_send, inspection_id, False)
        self._send_payload(request_id, payload)
        self._schedule_ack_timeout(request_id)

    async def reconcile_once(self) -> int | None:
        return await asyncio.to_thread(self._next_unsent_inspection_id)

    def schedule_ack(self, ack: PreRoofViewInspectionAckV02) -> None:
        self._track(asyncio.create_task(self._handle_ack(ack)))

    def schedule_result(self, result: PreRoofViewInspectionResultV02) -> None:
        self._track(asyncio.create_task(self._handle_result(result)))

    async def _handle_ack(self, ack: PreRoofViewInspectionAckV02) -> None:
        try:
            applied, view_request_id = await asyncio.to_thread(self._apply_ack_sync, ack)
            if view_request_id is not None and (applied or not ack.accepted):
                self._cancel_retry(view_request_id)
        except Exception:
            logger.exception("PRE_ROOF ACK handling failed request_id=%s", ack.inspection_request_id)

    async def _handle_result(self, result: PreRoofViewInspectionResultV02) -> None:
        try:
            disposition, view_request_id = await asyncio.to_thread(self._apply_result_sync, result)
            if view_request_id is not None and disposition in {PreRoofVisionApplyDisposition.APPLIED, PreRoofVisionApplyDisposition.DUPLICATE}:
                self._cancel_retry(view_request_id)
        except Exception:
            logger.exception("PRE_ROOF result handling failed request_id=%s", result.inspection_request_id)

    def is_expected_vision_source(self, addr: tuple[str, int] | tuple[Any, ...]) -> bool:
        if len(addr) < 2 or not isinstance(addr[0], str) or not isinstance(addr[1], int): return False
        try:
            expected = {entry[4][0] for entry in socket.getaddrinfo(self._config.vision_host, self._config.vision_port, type=socket.SOCK_DGRAM)}
        except socket.gaierror:
            return False
        return addr[0] in expected and addr[1] == self._config.vision_port

    def _require_started(self) -> None:
        if self._sender_transport is None: raise PreRoofUdpRuntimeError("PRE_ROOF UDP runtime has not been started.")

    def _next_unsent_inspection_id(self) -> int | None:
        with self._session_factory() as session:
            return session.scalar(select(ProductionInspection.inspection_id).join(ProductionInspectionViewRequest).where(
                ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
                ProductionInspection.status == ProductionInspectionStatus.RUNNING,
                ProductionInspectionViewRequest.status == "REQUESTED",
                ProductionInspectionViewRequest.sent_at.is_(None),
            ).order_by(ProductionInspectionViewRequest.view_request_id).limit(1))

    def _sent_unacked_view_request_ids(self) -> list[int]:
        with self._session_factory() as session:
            return list(session.scalars(select(ProductionInspectionViewRequest.view_request_id).join(ProductionInspection).where(
                ProductionInspection.status == ProductionInspectionStatus.RUNNING,
                ProductionInspectionViewRequest.status == "SENT",
                ProductionInspectionViewRequest.acked_at.is_(None),
            )))

    def _prepare_send(self, inspection_id: int, retry: bool) -> tuple[int, bytes]:
        with self._session_factory() as session:
            view_request_id, request = ProductionCompletionService(session).prepare_pre_roof_view_wire_request(inspection_id=inspection_id)
        with self._session_factory() as session:
            row = session.scalar(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.view_request_id == view_request_id).with_for_update())
            if row is None or row.status not in {"REQUESTED", "SENT", "ACKED"}: raise PreRoofUdpRuntimeError("PRE_ROOF view is no longer sendable.")
            if retry:
                if row.retry_count >= self._config.max_retries: raise PreRoofUdpRuntimeError("PRE_ROOF ACK retries are exhausted.")
                row.retry_count += 1
            elif row.sent_at is None:
                row.sent_at = datetime.now(timezone.utc); row.status = "SENT"
            snapshot = row.request_snapshot_json
            session.commit()
            self._notify_after_commit(row.inspection_id)
            assert snapshot is not None
            return row.view_request_id, snapshot.encode("utf-8")

    def _send_payload(self, view_request_id: int, payload: bytes) -> None:
        try:
            assert self._sender_transport is not None
            self._sender_transport.sendto(payload)
        except Exception as exc:
            self._mark_view_failed(view_request_id, "UDP_SEND_FAILED")
            raise PreRoofUdpRuntimeError("PRE_ROOF UDP send failed.") from exc

    def _schedule_ack_timeout(self, view_request_id: int) -> None:
        self._cancel_retry(view_request_id)
        self._retry_tasks[view_request_id] = asyncio.create_task(self._retry_after_timeout(view_request_id))

    async def _retry_after_timeout(self, view_request_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.ack_timeout_seconds)
                exhausted, payload = await asyncio.to_thread(self._prepare_retry, view_request_id)
                if exhausted or payload is None: return
                self._send_payload(view_request_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PRE_ROOF retry failed request=%s", view_request_id)
            self._mark_view_failed(view_request_id, "UDP_RETRY_RUNTIME_ERROR")
        finally:
            self._retry_tasks.pop(view_request_id, None)

    def _prepare_retry(self, view_request_id: int) -> tuple[bool, bytes | None]:
        with self._session_factory() as session:
            row = session.scalar(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.view_request_id == view_request_id).with_for_update())
            if row is None or row.status != "SENT" or row.acked_at is not None: return False, None
            if row.retry_count >= self._config.max_retries:
                inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.inspection_id == row.inspection_id).with_for_update())
                row.status = "FAILED"; row.error_code = "ACK_TIMEOUT_MAX_RETRIES"
                if inspection is not None and inspection.status is ProductionInspectionStatus.RUNNING:
                    inspection.status = ProductionInspectionStatus.ERROR
                    inspection.failure_reason = "ACK_TIMEOUT_MAX_RETRIES"
                    inspection.completed_at = datetime.now(timezone.utc)
                    session.commit(); self._notify_after_commit(inspection.inspection_id)
                else:
                    session.rollback()
                return True, None
            inspection_id = row.inspection_id
        _, payload = self._prepare_send(inspection_id, True)
        return False, payload

    def _apply_ack_sync(self, ack: PreRoofViewInspectionAckV02) -> tuple[bool, int | None]:
        with self._session_factory() as session:
            row = session.scalar(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.inspection_request_id == str(ack.inspection_request_id)))
            view_request_id = None if row is None else row.view_request_id
        with self._session_factory() as session:
            applied = ProductionCompletionService(session).apply_pre_roof_view_ack(
                inspection_request_id=str(ack.inspection_request_id), inspection_cycle=ack.inspection_cycle,
                view_name=ack.view_name.value, accepted=ack.accepted, duplicate=ack.duplicate, reason_code=ack.reason_code,
            )
        return applied, view_request_id

    def _apply_result_sync(self, result: PreRoofViewInspectionResultV02) -> tuple[PreRoofVisionApplyDisposition, int | None]:
        with self._session_factory() as session:
            row = session.scalar(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.inspection_request_id == str(result.inspection_request_id)))
            view_request_id = None if row is None else row.view_request_id
        with self._session_factory() as session:
            outcome = ProductionCompletionService(session).apply_pre_roof_view_result(vision_result=result, result_digest=pre_roof_result_semantic_digest(result))
        return outcome, view_request_id

    def _mark_view_failed(self, view_request_id: int, code: str) -> None:
        with self._session_factory() as session:
            row = session.scalar(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.view_request_id == view_request_id).with_for_update())
            if row is None or row.status in {"COMPLETED", "FAILED"}: return
            inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.inspection_id == row.inspection_id).with_for_update())
            row.status = "FAILED"; row.error_code = code
            if inspection is not None and inspection.status is ProductionInspectionStatus.RUNNING:
                inspection.status = ProductionInspectionStatus.ERROR; inspection.failure_reason = code; inspection.completed_at = datetime.now(timezone.utc)
                session.commit(); self._notify_after_commit(inspection.inspection_id)
            else: session.rollback()

    @staticmethod
    def _notify_after_commit(inspection_id: int) -> None:
        callback = get_production_inspection_change_callback()
        if callback is not None:
            try: callback(inspection_id)
            except Exception: logger.exception("ProductionInspection post-commit notification failed id=%s", inspection_id)

    def _cancel_retry(self, view_request_id: int) -> None:
        task = self._retry_tasks.pop(view_request_id, None)
        if task is not None and task is not asyncio.current_task(): task.cancel()

    def _track(self, task: asyncio.Task[None]) -> None:
        self._message_tasks.add(task); task.add_done_callback(self._message_tasks.discard)
