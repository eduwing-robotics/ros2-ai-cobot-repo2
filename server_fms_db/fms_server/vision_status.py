"""Async UDP intake and in-memory latest-state storage for global Vision health.

The Vision PC pushes UTF-8 JSON to the FMS at a low fixed rate. This module receives
only ``global_vision_status`` packets; it does not persist heartbeats, alter production
state, publish ROS messages, or send UDP replies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from shared.schemas.vision import VisionStatus, VisionStatusMessage

logger = logging.getLogger(__name__)
_VALIDATION_PAYLOAD_PREVIEW_LIMIT = 512


def _format_validation_errors(error: ValidationError) -> str:
    """Render Pydantic errors without including rejected input values or payload text."""

    summaries: list[str] = []
    for detail in error.errors(include_url=False):
        location = ".".join(str(part) for part in detail.get("loc", ())) or "<root>"
        reason = str(detail.get("msg", "validation failed"))
        summaries.append(f"field={location} reason={reason}")
    return "; ".join(summaries) or "field=<root> reason=validation failed"


@dataclass(frozen=True, slots=True)
class VisionStatusSnapshot:
    """Latest accepted status plus the time that this FMS process received it."""

    camera_id: str
    status: VisionStatus
    frame_seq: int
    last_frame_age_sec: float
    inference_error_count: int
    last_error: str | None
    status_stamp_sec: int
    status_stamp_nanosec: int
    server_received_at: datetime

    @classmethod
    def from_message(
        cls,
        message: VisionStatusMessage,
        *,
        server_received_at: datetime,
    ) -> "VisionStatusSnapshot":
        return cls(
            camera_id=message.camera_id,
            status=message.status,
            frame_seq=message.frame_seq,
            last_frame_age_sec=message.last_frame_age_sec,
            inference_error_count=message.inference_error_count,
            last_error=message.last_error,
            status_stamp_sec=message.status_stamp_sec,
            status_stamp_nanosec=message.status_stamp_nanosec,
            server_received_at=server_received_at,
        )


class VisionStatusStore:
    """Keep only the newest valid status for each camera in FMS process memory."""

    def __init__(self) -> None:
        self._latest_by_camera: dict[str, VisionStatusSnapshot] = {}

    def update(
        self,
        message: VisionStatusMessage,
        *,
        server_received_at: datetime | None = None,
    ) -> VisionStatusSnapshot:
        received_at = server_received_at or datetime.now(timezone.utc)
        snapshot = VisionStatusSnapshot.from_message(
            message,
            server_received_at=received_at,
        )
        self._latest_by_camera[snapshot.camera_id] = snapshot
        return snapshot

    def get(self, camera_id: str) -> VisionStatusSnapshot | None:
        """Return a camera's latest status, or ``None`` before its first packet."""

        return self._latest_by_camera.get(camera_id)

    def get_all(self) -> dict[str, VisionStatusSnapshot]:
        """Return a shallow copy so callers cannot mutate the internal mapping."""

        return dict(self._latest_by_camera)


class VisionStatusHandler:
    """Small handoff point between transport parsing and future FMS business logic."""

    def __init__(self, store: VisionStatusStore | None = None) -> None:
        self._store = store or VisionStatusStore()

    @property
    def store(self) -> VisionStatusStore:
        return self._store

    def handle(self, message: VisionStatusMessage) -> VisionStatusSnapshot:
        """Record an accepted packet without applying production-side behavior."""

        return self._store.update(message)


class VisionStatusDatagramProtocol(asyncio.DatagramProtocol):
    """Decode, validate, and hand off one UDP datagram without blocking the loop."""

    def __init__(self, handler: VisionStatusHandler) -> None:
        self._handler = handler

    def datagram_received(self, data: bytes, addr: tuple[str, int] | tuple[Any, ...]) -> None:
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Invalid vision UDP packet from %s: UTF-8 decode failed", addr)
            return

        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            logger.warning("Invalid vision UDP packet from %s: JSON parse failed", addr)
            return

        if not isinstance(payload, dict):
            logger.warning("Invalid vision UDP packet from %s: JSON object required", addr)
            return

        if payload.get("message_type") != "global_vision_status":
            logger.warning("Invalid vision UDP packet from %s: unexpected message_type", addr)
            return

        try:
            message = VisionStatusMessage.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "Invalid vision UDP packet from %s: schema validation failed: %s",
                addr,
                _format_validation_errors(exc),
            )
            # Keep rejected payload content out of WARNING logs. A short DEBUG preview
            # is available only when a developer explicitly enables debug logging.
            logger.debug(
                "Invalid vision UDP packet payload preview from %s (%s bytes): %r",
                addr,
                len(data),
                decoded[:_VALIDATION_PAYLOAD_PREVIEW_LIMIT],
            )
            return

        try:
            previous = self._handler.store.get(message.camera_id)
            snapshot = self._handler.handle(message)
        except Exception:  # pragma: no cover - defensive boundary around datagram callbacks
            logger.exception("Failed to handle vision UDP packet from %s", addr)
            return

        # Vision sends at 1 Hz. Log an informational line only on initial receipt or
        # state transition; repeated heartbeats remain available at DEBUG level.
        if previous is None or previous.status != snapshot.status:
            logger.info(
                "Vision status received: camera=%s status=%s frame_seq=%s",
                snapshot.camera_id,
                snapshot.status,
                snapshot.frame_seq,
            )
        else:
            logger.debug(
                "Vision status heartbeat: camera=%s status=%s frame_seq=%s",
                snapshot.camera_id,
                snapshot.status,
                snapshot.frame_seq,
            )

    def error_received(self, exc: Exception) -> None:
        logger.warning("Vision UDP receiver socket error: %s", exc)


class VisionStatusUdpReceiver:
    """Own the asyncio UDP transport for the Vision PC → FMS status channel."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        handler: VisionStatusHandler | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._handler = handler or VisionStatusHandler()
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def handler(self) -> VisionStatusHandler:
        return self._handler

    @property
    def is_running(self) -> bool:
        return self._transport is not None

    @property
    def bound_port(self) -> int | None:
        if self._transport is None:
            return None
        socket = self._transport.get_extra_info("socket")
        if socket is None:
            return None
        return int(socket.getsockname()[1])

    async def start(self) -> None:
        """Bind a non-blocking UDP endpoint once for this FMS process."""

        if self._transport is not None:
            return

        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: VisionStatusDatagramProtocol(self._handler),
            local_addr=(self._host, self._port),
        )
        self._transport = transport
        logger.info(
            "Vision UDP receiver listening on %s:%s",
            self._host,
            self.bound_port,
        )

    def close(self) -> None:
        """Close the UDP socket during the FMS shutdown sequence."""

        if self._transport is None:
            return
        self._transport.close()
        self._transport = None
        logger.info("Vision UDP receiver stopped")
