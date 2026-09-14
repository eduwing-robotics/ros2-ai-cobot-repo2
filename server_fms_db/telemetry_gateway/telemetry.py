"""ROS-independent telemetry normalization and latest-value Redis handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fms_server.cell_status import CellStatusValidationError, parse_cell_status_payload
from shared.equipment_registry import equipment
from shared.realtime.redis_client import RealtimeRedisClient, RedisUnavailableError

logger = logging.getLogger(__name__)

JOINT_STATE_CHANNEL = "telemetry.robot_joint_state"
STATUS_CHANNEL = "telemetry.robot_status"
MOBILE_ROBOT_POSE_CHANNEL = "telemetry.mobile_robot_pose"
FR5_ROBOT_ID = equipment("fr5").robot_id
FR5_MAX_OUTPUT_HZ = 20.0
FR5_MIN_OUTPUT_INTERVAL_SECONDS = 1.0 / FR5_MAX_OUTPUT_HZ

TelemetryKind = Literal["joint_state", "status", "ready", "mobile_robot_pose"]


@dataclass(frozen=True, slots=True)
class TelemetryUpdate:
    """One normalized latest-state update; never a durable production event."""

    kind: TelemetryKind
    robot_id: str
    payload: dict[str, Any]


class RedisTelemetrySink:
    """Write latest telemetry JSON then publish that same payload to Redis."""

    def __init__(
        self,
        client: RealtimeRedisClient,
        *,
        key_namespace: str = "telemetry",
        channel_prefix: str = "",
    ) -> None:
        self._client = client
        self._key_namespace = key_namespace.rstrip(":")
        self._channel_prefix = channel_prefix
        self.redis_available: bool | None = None

    async def publish(self, update: TelemetryUpdate) -> None:
        key = self.key_for(update)
        channel = self.channel_for(update)
        try:
            await self._client.set_json(key, update.payload)
            await self._client.publish_json(channel, update.payload)
        except RedisUnavailableError:
            self.redis_available = False
            raise
        self.redis_available = True

    def key_for(self, update: TelemetryUpdate) -> str:
        return f"{self._key_namespace}:robot:{update.robot_id}:{update.kind}"

    def channel_for(self, update: TelemetryUpdate) -> str:
        if update.kind == "joint_state":
            base = JOINT_STATE_CHANNEL
        elif update.kind == "mobile_robot_pose":
            base = MOBILE_ROBOT_POSE_CHANNEL
        else:
            base = STATUS_CHANNEL
        return f"{self._channel_prefix}{base}"


def utc_now_rfc3339_millis() -> str:
    """Return a timezone-explicit server receive timestamp."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ros_stamp_to_rfc3339(stamp: object | None) -> str | None:
    """Return a ROS Header stamp only when it is non-zero and representable."""

    if stamp is None:
        return None
    seconds = getattr(stamp, "sec", 0)
    nanoseconds = getattr(stamp, "nanosec", 0)
    if not isinstance(seconds, int) or not isinstance(nanoseconds, int) or (seconds == 0 and nanoseconds == 0):
        return None
    try:
        value = datetime.fromtimestamp(seconds + nanoseconds / 1_000_000_000, UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_joint_state(message: object, *, robot_id: str, received_at: str | None = None) -> dict[str, Any]:
    """Normalize a duck-typed ``sensor_msgs/msg/JointState`` without reordering."""

    header = getattr(message, "header", None)
    return {
        "robot_id": robot_id,
        "joint_names": list(getattr(message, "name", [])),
        "positions": list(getattr(message, "position", [])),
        "velocities": list(getattr(message, "velocity", [])),
        "efforts": list(getattr(message, "effort", [])),
        "source_timestamp": ros_stamp_to_rfc3339(getattr(header, "stamp", None)),
        "received_at": received_at or utc_now_rfc3339_millis(),
    }


def _finite_coordinate(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def normalize_mobile_robot_pose(
    message: object, *, robot_id: str, received_at: str | None = None
) -> dict[str, Any] | None:
    """Normalize one ``geometry_msgs/msg/PoseStamped`` without changing frames.

    Invalid samples are discarded. In particular, this boundary neither converts
    ``map`` to ``odom`` nor normalizes the quaternion: it preserves the source
    pose only when all numeric fields are finite and the quaternion is non-zero.
    """

    header = getattr(message, "header", None)
    frame_id = getattr(header, "frame_id", None)
    timestamp = ros_stamp_to_rfc3339(getattr(header, "stamp", None))
    pose = getattr(message, "pose", None)
    position = getattr(pose, "position", None)
    orientation = getattr(pose, "orientation", None)
    if not isinstance(frame_id, str) or not frame_id.strip() or timestamp is None:
        return None
    xyz = tuple(_finite_coordinate(getattr(position, axis, None)) for axis in ("x", "y", "z"))
    xyzw = tuple(_finite_coordinate(getattr(orientation, axis, None)) for axis in ("x", "y", "z", "w"))
    if any(value is None for value in (*xyz, *xyzw)):
        return None
    # A zero quaternion has no orientation. Do not repair it by normalization.
    if sum(value * value for value in xyzw if value is not None) == 0.0:
        return None
    return {
        "robot_id": robot_id,
        "frame_id": frame_id,
        "timestamp": timestamp,
        "position": {"x": xyz[0], "y": xyz[1], "z": xyz[2]},
        "orientation": {"x": xyzw[0], "y": xyzw[1], "z": xyzw[2], "w": xyzw[3]},
        "received_at": received_at or utc_now_rfc3339_millis(),
    }


def is_valid_mobile_robot_pose_payload(payload: object) -> bool:
    """Validate cached/PubSub pose shape before Unity fan-out."""

    if not isinstance(payload, dict):
        return False
    if not all(isinstance(payload.get(key), str) and payload[key] for key in ("robot_id", "frame_id", "timestamp")):
        return False
    try:
        parsed = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    position = payload.get("position")
    orientation = payload.get("orientation")
    if not isinstance(position, dict) or not isinstance(orientation, dict):
        return False
    xyz = tuple(_finite_coordinate(position.get(axis)) for axis in ("x", "y", "z"))
    xyzw = tuple(_finite_coordinate(orientation.get(axis)) for axis in ("x", "y", "z", "w"))
    return not any(value is None for value in (*xyz, *xyzw)) and sum(
        value * value for value in xyzw if value is not None
    ) != 0.0


def normalize_zk_status(raw_value: str, *, robot_id: str, received_at: str | None = None) -> dict[str, Any]:
    """Preserve valid ZK status JSON and make malformed payloads diagnostic-safe."""

    timestamp = received_at or utc_now_rfc3339_millis()
    try:
        decoded = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return {
            "robot_id": robot_id,
            "received_at": timestamp,
            "valid_json": False,
            "raw": raw_value,
        }
    if not isinstance(decoded, dict):
        return {
            "robot_id": robot_id,
            "received_at": timestamp,
            "valid_json": False,
            "raw": raw_value,
        }

    normalized = dict(decoded)
    normalized["robot_id"] = robot_id
    normalized["received_at"] = timestamp
    normalized["valid_json"] = True
    # /zkbotX/status.online is the authoritative connected source. Do not infer
    # ready from it or combine it with /zkbotX/ready.
    if isinstance(decoded.get("online"), bool):
        normalized["connected"] = decoded["online"]
    return normalized


def normalize_zk_ready(value: bool, *, robot_id: str, received_at: str | None = None) -> dict[str, Any]:
    """Normalize only the authoritative ``/zkbotX/ready`` Bool source."""

    return {
        "robot_id": robot_id,
        "ready": bool(value),
        "received_at": received_at or utc_now_rfc3339_millis(),
    }


def _nullable_grip(value: object) -> int | None:
    """Accept only the Robot Cell's nullable FR5 SDK opening scale."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value


def _nullable_grip_age(value: object) -> float | None:
    """Accept only finite, non-negative measured-position age values."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def normalize_fr5_cell_status(
    raw_value: str | bytes | Mapping[str, Any], *, received_at: str | None = None
) -> dict[str, Any] | None:
    """Project the additive FR5 gripper subset from the canonical Cell-status parser.

    Cell status remains the source for these observability fields only. This
    function deliberately does not infer grasp, pickup, or production state.
    """

    try:
        cell_status = parse_cell_status_payload(raw_value)
    except CellStatusValidationError:
        return None

    robots = cell_status.get("robots")
    fr5 = robots.get("fr5") if isinstance(robots, dict) else None
    fr5 = fr5 if isinstance(fr5, dict) else {}
    payload: dict[str, Any] = {
        "robot_id": FR5_ROBOT_ID,
        "grip": _nullable_grip(fr5.get("grip")),
        "grip_real": _nullable_grip(fr5.get("grip_real")),
        # Keep a numeric age even when grip_real is intentionally null.
        "grip_real_age_s": _nullable_grip_age(fr5.get("grip_real_age_s")),
        "received_at": received_at or utc_now_rfc3339_millis(),
    }
    if isinstance(fr5.get("connected"), bool):
        payload["connected"] = fr5["connected"]
    return payload


class LatestValueTelemetryHandoff:
    """Bounded ROS-thread to asyncio handoff with latest-value semantics.

    At most one pending update per robot/kind is retained. FR5 joint updates are
    downsampled at output time, so a delayed Redis path always sends the newest
    sample rather than a backlog of older frames.
    """

    def __init__(
        self,
        sink: RedisTelemetrySink | Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        fr5_min_output_interval_seconds: float = FR5_MIN_OUTPUT_INTERVAL_SECONDS,
        redis_retry_seconds: float = 1.0,
    ) -> None:
        self._sink = sink
        self._monotonic = monotonic
        self._fr5_interval = fr5_min_output_interval_seconds
        self._redis_retry_seconds = redis_retry_seconds
        self._pending: dict[tuple[TelemetryKind, str], TelemetryUpdate] = {}
        self._latest: dict[tuple[TelemetryKind, str], dict[str, Any]] = {}
        self._last_published_at: dict[tuple[TelemetryKind, str], float] = {}
        self._retry_not_before = 0.0
        self._last_failure_log_at = float("-inf")
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def offer(self, update: TelemetryUpdate) -> None:
        """Accept an update on the asyncio loop; replace any older pending one."""

        key = (update.kind, update.robot_id)
        self._latest[key] = update.payload
        self._pending[key] = update
        self._wake.set()

    def latest_payloads(self) -> list[dict[str, Any]]:
        """Return in-memory diagnostics only; this is not a Unity schema."""

        return [self._latest[key] for key in sorted(self._latest)]

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telemetry-redis-publisher")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def flush_due(self) -> int:
        """Publish eligible pending latest values once; exposed for deterministic tests."""

        now = self._monotonic()
        if now < self._retry_not_before:
            return 0
        published = 0
        for key, update in tuple(self._pending.items()):
            if self._remaining_delay(key, now) > 0:
                continue
            try:
                await self._sink.publish(update)
            except RedisUnavailableError as exc:
                self._retry_not_before = now + self._redis_retry_seconds
                if now - self._last_failure_log_at >= self._redis_retry_seconds:
                    logger.warning("Redis telemetry publish deferred: %s", exc)
                    self._last_failure_log_at = now
                return published
            self._pending.pop(key, None)
            self._last_published_at[key] = now
            self._retry_not_before = 0.0
            published += 1
        return published

    def next_wait_seconds(self) -> float | None:
        """Return delay until the next eligible pending item, or ``None`` if idle."""

        if not self._pending:
            return None
        now = self._monotonic()
        if now < self._retry_not_before:
            return self._retry_not_before - now
        return max(0.0, min(self._remaining_delay(key, now) for key in self._pending))

    def _remaining_delay(self, key: tuple[TelemetryKind, str], now: float) -> float:
        kind, robot_id = key
        if kind != "joint_state" or robot_id != FR5_ROBOT_ID:
            return 0.0
        return max(0.0, self._last_published_at.get(key, float("-inf")) + self._fr5_interval - now)

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.flush_due()
            self._wake.clear()
            # Any offer scheduled while flush awaited is already pending; do not
            # sleep before processing it.
            if self._pending and self.next_wait_seconds() == 0:
                await asyncio.sleep(0)
                continue
            timeout = self.next_wait_seconds()
            try:
                if timeout is None:
                    await self._wake.wait()
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass
