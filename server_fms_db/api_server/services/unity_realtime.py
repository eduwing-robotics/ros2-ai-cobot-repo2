"""Redis subscriber and per-connection Unity WebSocket fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from shared.config import get_settings
from shared.equipment_registry import equipment
from shared.realtime.production_events import PRODUCTION_CHANGED_CHANNEL
from shared.realtime.incoming_qa_events import INCOMING_QA_CHANGED_CHANNEL
from shared.realtime.production_inspection_events import PRODUCTION_INSPECTION_CHANGED_CHANNEL
from shared.realtime.error_events import ERROR_EVENT_CHANNEL
from shared.realtime.transport_events import TRANSPORT_EVENT_CHANNEL
from fms_server.forklift_runtime_state import ForkliftRuntimeEvent
from shared.services.unity_transport_status_projection_service import UnityTransportStatusProjectionService

from shared.realtime.redis_client import RedisUnavailableError, RealtimeRedisClient, create_realtime_redis_client
from telemetry_gateway.telemetry import (
    JOINT_STATE_CHANNEL,
    MOBILE_ROBOT_POSE_CHANNEL,
    STATUS_CHANNEL,
    is_valid_mobile_robot_pose_payload,
    utc_now_rfc3339_millis,
)

logger = logging.getLogger(__name__)

UNITY_SCHEMA_VERSION = "1.0"
FR5 = equipment("fr5")
ZKBOT1 = equipment("zkbot1")
ZKBOT2 = equipment("zkbot2")
FORKLIFT = equipment(get_settings().turtlebot_robot_id)
JOINT_KEYS = tuple(f"telemetry:robot:{robot.robot_id}:joint_state" for robot in (FR5, ZKBOT1, ZKBOT2))
STATUS_KEYS = tuple(
    f"telemetry:robot:{robot.robot_id}:{kind}"
    for robot, kinds in ((FR5, ("status",)), (ZKBOT1, ("status", "ready")), (ZKBOT2, ("status", "ready")))
    for kind in kinds
)
MOBILE_POSE_KEYS = (f"telemetry:robot:{FORKLIFT.robot_id}:mobile_robot_pose",)
MOBILE_POSE_ONLINE_WINDOW_SECONDS = 1.0
_MOBILE_STATUS_KIND = "mobile_status"


@dataclass(eq=False)
class UnityConnection:
    websocket: WebSocket
    sequence: int = 0
    syncing: bool = True
    pending_kinds: set[tuple[str, str]] = field(default_factory=set)
    pending_production: dict[int, dict[str, Any]] = field(default_factory=dict)
    production_events: asyncio.Queue[tuple[str, dict[str, Any]]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=32)
    )
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender_task: asyncio.Task[None] | None = None
    production_sender_task: asyncio.Task[None] | None = None


class UnityRealtimeHub:
    """Connection-scoped sequencing and latest-value telemetry fan-out."""

    def __init__(
        self,
        production_snapshot: Callable[[], dict[str, Any]],
        *,
        voice_snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._production_snapshot = production_snapshot
        self._voice_snapshot = voice_snapshot
        self._connections: set[UnityConnection] = set()
        self._joint_states: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self._readies: dict[str, dict[str, Any]] = {}
        self._mobile_poses: dict[str, dict[str, Any]] = {}
        self._active_transport_statuses: dict[str, dict[str, Any]] = {}
        self._mobile_status_signatures: dict[str, tuple[object, ...]] = {}

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> UnityConnection:
        await websocket.accept()
        connection = UnityConnection(websocket)
        self._connections.add(connection)
        # Snapshot has to be the first application envelope for every connection.
        snapshot = self._safe_snapshot()
        snapshot["robots"] = self._robot_overview()
        await self._send(connection, "production_snapshot", snapshot)
        # Existing mirror values are emitted as initial telemetry, so pending
        # updates received before this point are already represented. Updates
        # arriving during the sends below remain pending for a second drain.
        connection.pending_kinds.clear()
        await self._send_latest_telemetry(connection)
        # A realtime update received during SYNCING is coalesced by kind. Drain
        # those latest values before atomically switching this client to LIVE.
        while connection.pending_kinds:
            pending = tuple(connection.pending_kinds)
            connection.pending_kinds.clear()
            for update_key in sorted(pending):
                await self._send_latest_kind(connection, update_key)
        for status in connection.pending_production.values():
            await self._send(connection, "production_status", status)
        connection.pending_production.clear()
        connection.syncing = False
        if not connection.production_events.empty():
            self._ensure_production_sender(connection)
        return connection

    def disconnect(self, connection: UnityConnection) -> None:
        if connection.production_sender_task is not None and not connection.production_sender_task.done():
            connection.production_sender_task.cancel()
        self._connections.discard(connection)
        if connection.sender_task is not None and not connection.sender_task.done():
            connection.sender_task.cancel()


    async def ingest(self, channel: str, payload: dict[str, Any]) -> None:
        """Validate/store a full Pub/Sub payload and fan out without Redis GET."""

        update_key = self._store(channel, payload)
        if update_key is None:
            return
        if update_key[0] == "mobile_pose":
            await self.refresh_mobile_robot_statuses()
        for connection in tuple(self._connections):
            if connection.syncing:
                connection.pending_kinds.add(update_key)
                continue
            self._schedule_live_send(connection, update_key)
    async def publish_production_status(self, status: dict[str, Any]) -> None:
        """Fan out a DB-authoritative lifecycle update without telemetry coalescing."""

        job_id = status.get("job_id")
        if not isinstance(job_id, int) or job_id < 1:
            logger.warning("Ignoring production status without a valid job_id.")
            return
        for connection in tuple(self._connections):
            if connection.syncing:
                connection.pending_production[job_id] = status
                continue
            try:
                connection.production_events.put_nowait(("production_status", status))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full production-event queue.")
                self.disconnect(connection)
                continue
            if connection.production_sender_task is None or connection.production_sender_task.done():
                connection.production_sender_task = asyncio.create_task(
                    self._drain_production_events(connection),
                    name="unity-production-client-sender",
                )

    async def publish_incoming_qa_status(self, status: dict[str, Any]) -> None:
        """Fan out one DB-authoritative Incoming QA transaction projection."""

        job_id = status.get("job_id")
        transaction = status.get("transaction")
        if (
            not isinstance(job_id, int)
            or job_id < 1
            or not isinstance(transaction, dict)
            or not isinstance(transaction.get("transaction_id"), int)
        ):
            logger.warning("Ignoring Incoming QA status without a valid job/transaction identity.")
            return
        for connection in tuple(self._connections):
            if connection.syncing:
                # The reconnect snapshot is a complete persisted projection.
                continue
            try:
                connection.production_events.put_nowait(("incoming_qa_status", status))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full production-event queue.")
                self.disconnect(connection)
                continue
            if connection.production_sender_task is None or connection.production_sender_task.done():
                connection.production_sender_task = asyncio.create_task(
                    self._drain_production_events(connection),
                    name="unity-production-client-sender",
                )

    async def publish_production_inspection_status(self, status: dict[str, Any]) -> None:
        """Fan out a latest-cycle canonical ProductionInspection projection."""
        job_id = status.get("job_id")
        inspection_type = status.get("inspection_type")
        inspection = status.get("inspection")
        if (
            not isinstance(job_id, int) or job_id < 1
            or not isinstance(inspection_type, str) or not inspection_type
            or not isinstance(inspection, dict)
            or not isinstance(inspection.get("inspection_id"), int)
        ):
            logger.warning("Ignoring ProductionInspection status without a valid identity.")
            return
        for connection in tuple(self._connections):
            if connection.syncing:
                # Snapshot contains the same latest-cycle projection.
                continue
            try:
                connection.production_events.put_nowait(("production_inspection_status", status))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full production-event queue.")
                self.disconnect(connection)
                continue
            if connection.production_sender_task is None or connection.production_sender_task.done():
                connection.production_sender_task = asyncio.create_task(
                    self._drain_production_events(connection),
                    name="unity-production-client-sender",
                )

    async def publish_error_event(self, error: dict[str, Any]) -> None:
        """Fan out one authoritative error transition without telemetry coalescing."""
        for connection in tuple(self._connections):
            if connection.syncing:
                # Snapshot reconstructs unresolved state; transition delivery begins once LIVE.
                continue
            try:
                connection.production_events.put_nowait(("error_event", error))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full reliable-event queue.")
                self.disconnect(connection)
                continue
            if connection.production_sender_task is None or connection.production_sender_task.done():
                connection.production_sender_task = asyncio.create_task(
                    self._drain_production_events(connection),
                    name="unity-production-client-sender",
                )

    async def publish_transport_status(self, status: dict[str, Any]) -> None:
        """Fan out one actual transport feedback or terminal result without telemetry coalescing."""
        self._update_mobile_transport_activity(status)
        await self.refresh_mobile_robot_statuses()
        for connection in tuple(self._connections):
            if connection.syncing:
                # Reconnect transport reconstruction is deliberately outside this live-event ticket.
                continue
            try:
                connection.production_events.put_nowait(("transport_status", status))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full reliable-event queue.")
                self.disconnect(connection)
                continue
            if connection.production_sender_task is None or connection.production_sender_task.done():
                connection.production_sender_task = asyncio.create_task(
                    self._drain_production_events(connection),
                    name="unity-production-client-sender",
                )

    async def publish_voice_event(self, event: dict[str, Any]) -> None:
        """Best-effort Voice observability on the existing Unity event queue."""
        state = event.get("state")
        if not isinstance(state, str) or not state:
            logger.warning("Ignoring Voice event without a state.")
            return
        for connection in tuple(self._connections):
            try:
                connection.production_events.put_nowait(("voice_runtime_event", event))
            except asyncio.QueueFull:
                logger.warning("Dropping slow Unity client with a full Voice event queue.")
                self.disconnect(connection)
                continue
            if not connection.syncing:
                self._ensure_production_sender(connection)

    def _ensure_production_sender(self, connection: UnityConnection) -> None:
        if connection.production_sender_task is None or connection.production_sender_task.done():
            connection.production_sender_task = asyncio.create_task(
                self._drain_production_events(connection),
                name="unity-production-client-sender",
            )

    def hydrate(
        self,
        *,
        joint_states: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        readies: list[dict[str, Any]],
        mobile_poses: list[dict[str, Any]] | None = None,
    ) -> None:
        mobile_poses = mobile_poses or []
        for payload in joint_states:
            robot_id = payload.get("robot_id")
            if isinstance(robot_id, str):
                self._joint_states[robot_id] = payload
        for payload in statuses:
            robot_id = payload.get("robot_id")
            if isinstance(robot_id, str):
                self._statuses[robot_id] = payload
        for payload in readies:
            robot_id = payload.get("robot_id")
            if isinstance(robot_id, str):
                self._readies[robot_id] = payload
        for payload in mobile_poses:
            robot_id = payload.get("robot_id")
            if isinstance(robot_id, str) and is_valid_mobile_robot_pose_payload(payload):
                self._mobile_poses[robot_id] = payload
    def _store(self, channel: str, payload: dict[str, Any]) -> tuple[str, str] | None:
        robot_id = payload.get("robot_id")
        if not isinstance(robot_id, str) or not robot_id:
            logger.warning("Ignoring telemetry without canonical robot_id on %s.", channel)
            return None
        if channel == JOINT_STATE_CHANNEL:
            self._joint_states[robot_id] = payload
            return ("joint", robot_id)
        if channel == STATUS_CHANNEL:
            if "ready" in payload and set(payload).issubset({"robot_id", "ready", "received_at"}):
                self._readies[robot_id] = payload
            else:
                self._statuses[robot_id] = payload
            return ("status", robot_id)
        if channel == MOBILE_ROBOT_POSE_CHANNEL:
            if not is_valid_mobile_robot_pose_payload(payload):
                logger.warning("Ignoring invalid mobile robot pose telemetry for robot_id=%s.", robot_id)
                return None
            self._mobile_poses[robot_id] = payload
            return ("mobile_pose", robot_id)
        logger.warning("Ignoring unsupported Redis telemetry channel %s.", channel)
        return None

    async def refresh_mobile_robot_statuses(self) -> None:
        """Emit only meaningful pose-connectivity/activity transitions.

        Pose transport remains independent: a stale pose never blocks the
        position stream, and a Unity client cannot affect any robot command.
        """
        robot_ids = set(self._mobile_poses) | set(self._active_transport_statuses)
        for robot_id in robot_ids:
            status = self._mobile_robot_status(robot_id)
            signature = (
                status["connection_state"],
                status["activity_state"],
                status.get("transport_phase"),
                status.get("transport_task_type"),
            )
            if self._mobile_status_signatures.get(robot_id) == signature:
                continue
            self._mobile_status_signatures[robot_id] = signature
            for connection in tuple(self._connections):
                connection.pending_kinds.add((_MOBILE_STATUS_KIND, robot_id))
                if not connection.syncing:
                    self._schedule_live_send(connection, (_MOBILE_STATUS_KIND, robot_id))

    def _update_mobile_transport_activity(self, status: dict[str, Any]) -> None:
        robot_id = status.get("robot_id")
        if not isinstance(robot_id, str) or robot_id != FORKLIFT.robot_id:
            return
        # Feedback with no terminal result represents the current FMS-owned
        # action. A terminal event clears it; independently operated robots
        # therefore remain IDLE unless FMS actually owns a goal.
        if status.get("result") is None:
            self._active_transport_statuses[robot_id] = dict(status)
        else:
            self._active_transport_statuses.pop(robot_id, None)

    def _mobile_robot_status(self, robot_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        pose = self._mobile_poses.get(robot_id)
        pose_age_s: float | None = None
        if pose is not None:
            received_at = pose.get("received_at")
            if isinstance(received_at, str):
                try:
                    received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                    if received.tzinfo is not None:
                        pose_age_s = max(0.0, ((now or datetime.now(UTC)) - received).total_seconds())
                except ValueError:
                    pass
        connected = pose_age_s is not None and pose_age_s <= MOBILE_POSE_ONLINE_WINDOW_SECONDS
        active = self._active_transport_statuses.get(robot_id)
        activity_state = "WORKING" if active is not None else "IDLE"
        result: dict[str, Any] = {
            "robot_id": robot_id,
            "connected": connected,
            "connection_state": "ONLINE" if connected else "CONNECTION_LOST",
            "activity_state": activity_state,
            "status": "CONNECTION_LOST" if not connected else activity_state,
            "pose_age_s": pose_age_s,
        }
        if pose is not None and isinstance(pose.get("timestamp"), str):
            result["last_pose_timestamp"] = pose["timestamp"]
        if active is not None:
            for source, target in (("task_type", "transport_task_type"), ("phase", "transport_phase")):
                value = active.get(source)
                if isinstance(value, str) and value:
                    result[target] = value
        return result

    async def _send_latest_telemetry(self, connection: UnityConnection) -> None:
        for robot_id in sorted(self._joint_states):
            await self._send(connection, "robot_joint_state", self._unity_joint(self._joint_states[robot_id]))
        for robot_id in sorted(set(self._statuses) | set(self._readies)):
            await self._send(connection, "robot_status", self._unity_status(robot_id))
        for robot_id in sorted(self._mobile_poses):
            await self._send(connection, "mobile_robot_pose", self._unity_mobile_pose(self._mobile_poses[robot_id]))
        for robot_id in sorted(set(self._mobile_poses) | set(self._active_transport_statuses)):
            await self._send(connection, "robot_status", self._mobile_robot_status(robot_id))

    async def _send_latest_kind(
        self, connection: UnityConnection, update_key: tuple[str, str]
    ) -> None:
        kind, robot_id = update_key
        if kind == "joint":
            payload = self._joint_states.get(robot_id)
            if payload is not None:
                await self._send(connection, "robot_joint_state", self._unity_joint(payload))
        elif kind == "status" and (robot_id in self._statuses or robot_id in self._readies):
            await self._send(connection, "robot_status", self._unity_status(robot_id))
        elif kind == "mobile_pose":
            payload = self._mobile_poses.get(robot_id)
            if payload is not None:
                await self._send(connection, "mobile_robot_pose", self._unity_mobile_pose(payload))
        elif kind == _MOBILE_STATUS_KIND:
            await self._send(connection, "robot_status", self._mobile_robot_status(robot_id))

    async def _drain_production_events(self, connection: UnityConnection) -> None:
        try:
            while connection in self._connections:
                message_type, data = await connection.production_events.get()
                await self._send(connection, message_type, data)
                if connection.production_events.empty():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Unity production-status send failed; dropping client: %s", exc)
            self.disconnect(connection)
        finally:
            connection.production_sender_task = None

    def _schedule_live_send(self, connection: UnityConnection, update_key: tuple[str, str]) -> None:
        """Coalesce telemetry per robot for a slow client without blocking others."""

        connection.pending_kinds.add(update_key)
        if connection.sender_task is None or connection.sender_task.done():
            connection.sender_task = asyncio.create_task(
                self._drain_live_sends(connection),
                name="unity-telemetry-client-sender",
            )

    async def _drain_live_sends(self, connection: UnityConnection) -> None:
        try:
            while connection.pending_kinds and connection in self._connections:
                update_keys = tuple(connection.pending_kinds)
                connection.pending_kinds.clear()
                for update_key in sorted(update_keys):
                    await self._send_latest_kind(connection, update_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Unity WebSocket send failed; dropping client: %s", exc)
            self.disconnect(connection)
        finally:
            connection.sender_task = None
    async def _send(self, connection: UnityConnection, message_type: str, data: dict[str, Any]) -> None:
        # The number advances only immediately before a concrete outbound send.
        async with connection.send_lock:
            sequence = connection.sequence + 1
            envelope = {
                "schema_version": UNITY_SCHEMA_VERSION,
                "type": message_type,
                "timestamp": utc_now_rfc3339_millis(),
                "sequence": sequence,
                "data": data,
            }
            await connection.websocket.send_json(envelope)
            connection.sequence = sequence

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            snapshot = self._production_snapshot()
        except Exception as exc:  # Redis must not be needed to preserve API availability.
            logger.warning("Production snapshot read unavailable: %s", exc)
            snapshot = {}
        result = {
            "jobs": snapshot.get("jobs", []),
            "robots": snapshot.get("robots", []),
            "transports": snapshot.get("transports", []),
            "incoming_qa": snapshot.get("incoming_qa", []),
            "production_inspections": snapshot.get("production_inspections", []),
            "active_errors": snapshot.get("active_errors", []),
        }
        if self._voice_snapshot is not None:
            try:
                result["voice_runtime"] = self._voice_snapshot()
            except Exception as exc:
                logger.warning("Voice snapshot read unavailable: %s", exc)
                result["voice_runtime"] = {
                    "state": "IDLE", "runtime_status": "IDLE",
                    "current_turn": None, "recent_turns": [],
                }
        return result

    def _robot_overview(self) -> list[dict[str, Any]]:
        robot_ids = set(self._statuses) | set(self._readies)
        robot_ids |= set(self._mobile_poses) | set(self._active_transport_statuses)
        return [self._unity_status(robot_id) for robot_id in sorted(robot_ids)]

    @staticmethod
    def _unity_joint(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "robot_id": payload["robot_id"],
            "joint_names": payload.get("joint_names", []),
            "positions": payload.get("positions", []),
            "velocities": payload.get("velocities", []),
            "efforts": payload.get("efforts", []),
            "source_timestamp": payload.get("source_timestamp"),
        }

    @staticmethod
    def _unity_mobile_pose(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "robot_id": payload["robot_id"],
            "frame_id": payload["frame_id"],
            "timestamp": payload["timestamp"],
            "position": dict(payload["position"]),
            "orientation": dict(payload["orientation"]),
        }

    def _unity_status(self, robot_id: str) -> dict[str, Any]:
        if robot_id in self._mobile_poses or robot_id in self._active_transport_statuses:
            return self._mobile_robot_status(robot_id)
        status = self._statuses.get(robot_id, {})
        ready = self._readies.get(robot_id, {})
        result: dict[str, Any] = {"robot_id": robot_id}
        if isinstance(status.get("connected"), bool):
            result["connected"] = status["connected"]
        if isinstance(ready.get("ready"), bool):
            result["ready"] = ready["ready"]
        if isinstance(status.get("busy"), bool):
            result["busy"] = status["busy"]
        if robot_id == FR5.robot_id:
            for field_name in ("grip", "grip_real"):
                value = status.get(field_name)
                if value is None and field_name in status:
                    result[field_name] = None
                elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
                    result[field_name] = value
            age = status.get("grip_real_age_s")
            if age is None and "grip_real_age_s" in status:
                result["grip_real_age_s"] = None
            elif isinstance(age, (int, float)) and not isinstance(age, bool):
                numeric_age = float(age)
                if math.isfinite(numeric_age) and numeric_age >= 0:
                    result["grip_real_age_s"] = numeric_age
        return result


class RedisTelemetrySubscriber:
    """Background Pub/Sub consumer with bounded reconnect and latest refresh."""

    def __init__(
        self,
        hub: UnityRealtimeHub,
        *,
        client_factory: Callable[[], RealtimeRedisClient] = create_realtime_redis_client,
        channel_prefix: str = "",
        production_status_reader: Callable[[int], dict[str, Any] | None] | None = None,
        error_event_reader: Callable[[int], dict[str, Any] | None] | None = None,
        incoming_qa_status_reader: Callable[[int], dict[str, Any] | None] | None = None,
        production_inspection_status_reader: Callable[[int], dict[str, Any] | None] | None = None,
    ) -> None:
        self._hub = hub
        self._client_factory = client_factory
        self._channel_prefix = channel_prefix
        self._joint_channel = f"{channel_prefix}{JOINT_STATE_CHANNEL}"
        self._mobile_pose_channel = f"{channel_prefix}{MOBILE_ROBOT_POSE_CHANNEL}"
        self._production_channel = f"{channel_prefix}{PRODUCTION_CHANGED_CHANNEL}"
        self._error_channel = f"{channel_prefix}{ERROR_EVENT_CHANNEL}"
        self._incoming_qa_channel = f"{channel_prefix}{INCOMING_QA_CHANGED_CHANNEL}"
        self._production_inspection_channel = f"{channel_prefix}{PRODUCTION_INSPECTION_CHANGED_CHANNEL}"
        self._transport_channel = f"{channel_prefix}{TRANSPORT_EVENT_CHANNEL}"
        self._production_status_reader = production_status_reader
        self._error_event_reader = error_event_reader
        self._incoming_qa_status_reader = incoming_qa_status_reader
        self._production_inspection_status_reader = production_inspection_status_reader
        self._status_channel = f"{channel_prefix}{STATUS_CHANNEL}"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: RealtimeRedisClient | None = None
        self.available: bool | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="unity-redis-telemetry-subscriber")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            client = self._client_factory()
            self._client = client
            pubsub = None
            try:
                if not await client.ping():
                    raise RedisUnavailableError("Redis PING failed.")
                await self._refresh_latest(client)
                pubsub = client.raw_client.pubsub()
                await pubsub.subscribe(
                    self._joint_channel, self._mobile_pose_channel, self._status_channel, self._production_channel,
                    self._error_channel, self._incoming_qa_channel, self._production_inspection_channel, self._transport_channel,
                )
                self.available = True
                backoff = 1.0
                while not self._stop.is_set():
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None:
                        await self._hub.refresh_mobile_robot_statuses()
                        continue
                    data = message.get("data")
                    channel = message.get("channel")
                    if not isinstance(data, str) or not isinstance(channel, str):
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("Ignoring invalid JSON on Redis channel %s.", channel)
                        continue
                    if not isinstance(payload, dict):
                        continue
                    canonical_channel = channel.removeprefix(self._channel_prefix)
                    if canonical_channel == PRODUCTION_CHANGED_CHANNEL:
                        await self._handle_production_changed(payload)
                    elif canonical_channel == ERROR_EVENT_CHANNEL:
                        await self._handle_error_event(payload)
                    elif canonical_channel == INCOMING_QA_CHANGED_CHANNEL:
                        await self._handle_incoming_qa_changed(payload)
                    elif canonical_channel == PRODUCTION_INSPECTION_CHANGED_CHANNEL:
                        await self._handle_production_inspection_changed(payload)
                    elif canonical_channel == TRANSPORT_EVENT_CHANNEL:
                        await self._handle_transport_event(payload)
                    else:
                        await self._hub.ingest(canonical_channel, payload)
            except (RedisUnavailableError, OSError, asyncio.TimeoutError) as exc:
                self.available = False
                logger.warning("Unity Redis subscriber unavailable: %s; retrying in %.0fs.", exc, backoff)
                await self._wait_or_stop(backoff)
                backoff = min(5.0, backoff * 2)
            except Exception as exc:
                self.available = False
                logger.warning("Unity Redis subscriber failed: %s; retrying in %.0fs.", exc, backoff)
                await self._wait_or_stop(backoff)
                backoff = min(5.0, backoff * 2)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                await client.close()
                self._client = None

    async def _handle_production_changed(self, payload: dict[str, Any]) -> None:
        raw_job_id = payload.get("job_id")
        try:
            job_id = int(raw_job_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring production.changed without a valid job_id.")
            return
        if job_id < 1 or self._production_status_reader is None:
            return
        try:
            status = await asyncio.to_thread(self._production_status_reader, job_id)
        except Exception as exc:
            logger.warning("Production status re-read failed for job_id=%s: %s", job_id, exc)
            return
        if status is None:
            logger.info("Ignoring production.changed for unknown job_id=%s.", job_id)
            return
        await self._hub.publish_production_status(status)

    async def _handle_incoming_qa_changed(self, payload: dict[str, Any]) -> None:
        raw_transaction_id = payload.get("transaction_id")
        try:
            transaction_id = int(raw_transaction_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring Incoming QA change without a valid transaction_id.")
            return
        if transaction_id < 1 or self._incoming_qa_status_reader is None:
            return
        try:
            status = await asyncio.to_thread(self._incoming_qa_status_reader, transaction_id)
        except Exception as exc:
            logger.warning(
                "Incoming QA status re-read failed for transaction_id=%s: %s", transaction_id, exc
            )
            return
        if status is not None:
            await self._hub.publish_incoming_qa_status(status)

    async def _handle_production_inspection_changed(self, payload: dict[str, Any]) -> None:
        raw_inspection_id = payload.get("inspection_id")
        try:
            inspection_id = int(raw_inspection_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring ProductionInspection change without a valid inspection_id.")
            return
        if inspection_id < 1 or self._production_inspection_status_reader is None:
            return
        try:
            status = await asyncio.to_thread(self._production_inspection_status_reader, inspection_id)
        except Exception as exc:
            logger.warning("ProductionInspection status re-read failed id=%s: %s", inspection_id, exc)
            return
        if status is not None:
            await self._hub.publish_production_inspection_status(status)

    async def _handle_error_event(self, payload: dict[str, Any]) -> None:
        raw_attempt_id = payload.get("attempt_id")
        try:
            attempt_id = int(raw_attempt_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring error.event without a valid attempt_id.")
            return
        if attempt_id < 1 or self._error_event_reader is None:
            return
        try:
            error = await asyncio.to_thread(self._error_event_reader, attempt_id)
        except Exception as exc:
            logger.warning("Error-event re-read failed for attempt_id=%s: %s", attempt_id, exc)
            return
        if error is not None:
            await self._hub.publish_error_event(error)

    async def _handle_transport_event(self, payload: dict[str, Any]) -> None:
        event = ForkliftRuntimeEvent.from_payload(payload)
        if event is None:
            logger.warning("Ignoring invalid transport.event payload.")
            return
        try:
            status = UnityTransportStatusProjectionService.project(event)
        except Exception as exc:
            logger.warning("Ignoring unprojectable transport.event: %s", exc)
            return
        await self._hub.publish_transport_status(status)

    async def _refresh_latest(self, client: RealtimeRedisClient) -> None:
        joints: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        readies: list[dict[str, Any]] = []
        mobile_poses: list[dict[str, Any]] = []
        for key in JOINT_KEYS:
            payload = await client.get_json(key)
            if isinstance(payload, dict):
                joints.append(payload)
        for key in MOBILE_POSE_KEYS:
            payload = await client.get_json(key)
            if isinstance(payload, dict):
                mobile_poses.append(payload)
        for key in STATUS_KEYS:
            payload = await client.get_json(key)
            if not isinstance(payload, dict):
                continue
            if key.endswith(":ready"):
                readies.append(payload)
            else:
                statuses.append(payload)
        self._hub.hydrate(
            joint_states=joints,
            statuses=statuses,
            readies=readies,
            mobile_poses=mobile_poses,
        )

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
