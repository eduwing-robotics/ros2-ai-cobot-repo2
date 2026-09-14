"""FMS-owned transient Robot Cell status and heartbeat support."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)
CELL_STATUS_TOPIC = "/cell/status"
CELL_STATUS_VERSION = "0.2"
CELL_STATUS_SUPPORTED_VERSIONS = frozenset({"0.2", "0.3"})
CELL_STATUS_STATES = frozenset({"IDLE", "EXECUTE", "HELD", "ABORTED", "FAULT"})
CELL_STATUS_HEARTBEAT_TIMEOUT_SECONDS = 3.0
_ACTIVE_TASK_FIELDS = frozenset({"req_id", "job_id", "step_id", "task_type", "phase", "current_item", "total_items", "progress", "robot"})
_HOLD_FIELDS = frozenset({"task_req_id", "pause_req_id", "stop_mode", "held_at", "phase", "since", "resumable"})
_HOLD_STOP_MODES = frozenset({"IMMEDIATE", "AT_PHASE_BOUNDARY", "DEFERRED_UNSAFE", "NOT_APPLICABLE"})
_HELD_AT_VALUES = frozenset({"PHASE_BOUNDARY", "STEP_BOUNDARY", "HOVER", "OBSERVE", "MID_MOTION"})

class CellStatusValidationError(ValueError):
    """A `/cell/status` payload does not satisfy the agreed v0.2 contract."""

@dataclass(frozen=True, slots=True)
class CellStatusSnapshot:
    seq: int
    remote_timestamp: str
    cell_state: str
    active_task: Mapping[str, Any] | None
    hold: Mapping[str, Any] | None
    robots: Mapping[str, Any] | None
    conveyor: Mapping[str, Any] | None
    error: str | None
    payload: Mapping[str, Any]
    received_monotonic: float

@dataclass(frozen=True, slots=True)
class CellDispatchAvailability:
    heartbeat_available: bool
    dispatch_allowed: bool
    reason: str
    snapshot: CellStatusSnapshot | None

def parse_cell_status_payload(raw_payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_payload, (str, bytes)):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CellStatusValidationError("/cell/status must be valid JSON.") from exc
    else:
        payload = dict(raw_payload)
    if not isinstance(payload, dict):
        raise CellStatusValidationError("/cell/status payload must be a JSON object.")
    if payload.get("ver") not in CELL_STATUS_SUPPORTED_VERSIONS:
        raise CellStatusValidationError("/cell/status ver is unsupported.")
    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise CellStatusValidationError("/cell/status seq must be a non-negative integer.")
    timestamp = payload.get("ts")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise CellStatusValidationError("/cell/status ts must be a non-empty string.")
    state = payload.get("cell_state")
    if not isinstance(state, str) or state not in CELL_STATUS_STATES:
        raise CellStatusValidationError("/cell/status cell_state is unsupported.")
    active_task = payload.get("active_task")
    if active_task is not None:
        if not isinstance(active_task, dict) or not _ACTIVE_TASK_FIELDS.issubset(active_task):
            raise CellStatusValidationError("/cell/status active_task is structurally invalid.")
        if any(not isinstance(active_task[name], str) or not active_task[name].strip() for name in ("req_id", "job_id", "step_id", "task_type", "phase", "robot")):
            raise CellStatusValidationError("/cell/status active_task string fields are invalid.")
        if any(isinstance(active_task[name], bool) or not isinstance(active_task[name], int) or active_task[name] < 0 for name in ("current_item", "total_items")):
            raise CellStatusValidationError("/cell/status active_task item fields are invalid.")
        progress = active_task["progress"]
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0.0 <= float(progress) <= 1.0:
            raise CellStatusValidationError("/cell/status active_task progress is invalid.")
    hold = payload.get("hold")
    if hold is not None:
        if not isinstance(hold, dict) or not _HOLD_FIELDS.issubset(hold):
            raise CellStatusValidationError("/cell/status hold is structurally invalid.")
        if any(not isinstance(hold[name], str) or not hold[name].strip() for name in ("task_req_id", "pause_req_id", "stop_mode", "held_at", "phase", "since")):
            raise CellStatusValidationError("/cell/status hold string fields are invalid.")
        if hold["stop_mode"] not in _HOLD_STOP_MODES or hold["held_at"] not in _HELD_AT_VALUES:
            raise CellStatusValidationError("/cell/status hold stop semantics are unsupported.")
        if not isinstance(hold["resumable"], bool):
            raise CellStatusValidationError("/cell/status hold resumable must be boolean.")
    for field_name in ("robots", "conveyor"):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, dict):
            raise CellStatusValidationError(f"/cell/status {field_name} must be an object when supplied.")
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise CellStatusValidationError("/cell/status error must be a string or null.")
    return payload

class CellStatusStore:
    """Thread-safe latest valid status; never mutates production state."""
    def __init__(self, *, heartbeat_timeout_seconds: float = CELL_STATUS_HEARTBEAT_TIMEOUT_SECONDS, monotonic_clock: Callable[[], float] = monotonic) -> None:
        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive.")
        self._timeout, self._monotonic = heartbeat_timeout_seconds, monotonic_clock
        self._latest: CellStatusSnapshot | None = None
        self._lock = threading.RLock()

    def accept(self, raw_payload: str | bytes | Mapping[str, Any]) -> CellStatusSnapshot:
        payload = parse_cell_status_payload(raw_payload)
        snapshot = CellStatusSnapshot(payload["seq"], payload["ts"], payload["cell_state"], payload.get("active_task"), payload.get("hold"), payload.get("robots"), payload.get("conveyor"), payload.get("error"), payload, self._monotonic())
        with self._lock:
            self._latest = snapshot
        return snapshot

    def latest(self) -> CellStatusSnapshot | None:
        with self._lock:
            return self._latest

    def heartbeat_available(self) -> bool:
        snapshot = self.latest()
        return snapshot is not None and self._monotonic() - snapshot.received_monotonic <= self._timeout

    def dispatch_availability(self) -> CellDispatchAvailability:
        snapshot = self.latest()
        if snapshot is None:
            return CellDispatchAvailability(False, False, "CELL_STATUS_NOT_RECEIVED", None)
        if self._monotonic() - snapshot.received_monotonic > self._timeout:
            return CellDispatchAvailability(False, False, "CELL_STATUS_STALE", snapshot)
        if snapshot.cell_state != "IDLE":
            return CellDispatchAvailability(True, False, f"CELL_STATE_{snapshot.cell_state}", snapshot)
        return CellDispatchAvailability(True, True, "CELL_READY", snapshot)

class RosCellStatusSubscriber:
    """One FMS-owned ROS node subscribing only to `/cell/status`."""
    def __init__(self, store: CellStatusStore) -> None:
        self._store = store
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: threading.Thread | None = None
        self._rclpy: Any | None = None
        self._owns_rclpy_init = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
            from std_msgs.msg import String
        except ImportError as exc:
            raise RuntimeError("rclpy and std_msgs are required for /cell/status subscription.") from exc
        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init(args=None); self._owns_rclpy_init = True
        self._node = Node(f"fms_cell_status_subscriber_{uuid.uuid4().hex}")
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST)
        self._node.create_subscription(String, CELL_STATUS_TOPIC, self._on_status, qos)
        self._executor = SingleThreadedExecutor(); self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, name="fms-cell-status-ros-executor", daemon=True)
        self._thread.start()
        logger.info("FMS Cell status subscriber started: topic=%s", CELL_STATUS_TOPIC)

    def close(self) -> None:
        if self._executor is None:
            return
        self._executor.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._node is not None:
            self._node.destroy_node()
        self._thread = self._node = self._executor = None
        if self._owns_rclpy_init and self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()
        self._owns_rclpy_init = False

    def _on_status(self, message: object) -> None:
        previous = self._store.latest()
        try:
            snapshot = self._store.accept(getattr(message, "data", ""))
        except CellStatusValidationError as exc:
            logger.warning("Ignoring invalid /cell/status heartbeat: %s", exc)
            return
        if previous is None or previous.cell_state != snapshot.cell_state:
            logger.info("Cell status received: seq=%s state=%s available=true", snapshot.seq, snapshot.cell_state)
        else:
            logger.debug("Cell status heartbeat received: seq=%s state=%s", snapshot.seq, snapshot.cell_state)
