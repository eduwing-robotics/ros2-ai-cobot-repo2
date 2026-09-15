"""Lazy ROS 2 telemetry subscriptions owned by the Telemetry Gateway."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any, Callable

from shared.config import get_settings
from shared.equipment_registry import equipment
from telemetry_gateway.telemetry import (
    TelemetryUpdate,
    normalize_fr5_cell_status,
    normalize_joint_state,
    normalize_mobile_robot_pose,
    normalize_zk_ready,
    normalize_zk_status,
)

logger = logging.getLogger(__name__)


class RosTelemetryUnavailableError(RuntimeError):
    """ROS 2 telemetry dependencies are unavailable in this process."""


class RosTelemetrySubscriber:
    """One private Node plus one SingleThreadedExecutor background thread."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        offer_update: Callable[[TelemetryUpdate], None],
    ) -> None:
        self._loop = loop
        self._offer_update = offer_update
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: threading.Thread | None = None
        self._rclpy: Any | None = None
        self._owns_rclpy_init = False
        self._status_ready: dict[str, bool] = {}
        self._ready_topic_value: dict[str, bool] = {}

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
            from geometry_msgs.msg import PoseStamped
            from sensor_msgs.msg import JointState
            from std_msgs.msg import Bool, String
        except ImportError as exc:
            raise RosTelemetryUnavailableError(
                "rclpy, geometry_msgs, sensor_msgs, and std_msgs must be available to enable ROS telemetry."
            ) from exc

        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init(args=None)
            self._owns_rclpy_init = True
        self._node = Node(f"telemetry_gateway_{uuid.uuid4().hex}")
        reliable_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
        )
        fr5_joint_state_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
        )
        fr5 = equipment("fr5")
        zkbot1 = equipment("zkbot1")
        zkbot2 = equipment("zkbot2")
        forklift = equipment(get_settings().turtlebot_robot_id)
        self._node.create_subscription(
            JointState, fr5.joint_state_topic, self._joint_callback(fr5.robot_id), fr5_joint_state_qos
        )
        self._node.create_subscription(JointState, zkbot1.joint_state_topic, self._joint_callback(zkbot1.robot_id), reliable_qos)
        self._node.create_subscription(JointState, zkbot2.joint_state_topic, self._joint_callback(zkbot2.robot_id), reliable_qos)
        self._node.create_subscription(String, zkbot1.status_topic, self._status_callback(zkbot1.robot_id), reliable_qos)
        self._node.create_subscription(String, zkbot2.status_topic, self._status_callback(zkbot2.robot_id), reliable_qos)
        self._node.create_subscription(String, "/cell/status", self._cell_status_callback(), reliable_qos)
        self._node.create_subscription(Bool, zkbot1.ready_topic, self._ready_callback(zkbot1.robot_id), reliable_qos)
        self._node.create_subscription(Bool, zkbot2.ready_topic, self._ready_callback(zkbot2.robot_id), reliable_qos)
        self._node.create_subscription(
            PoseStamped,
            forklift.mobile_robot_pose_topic,
            self._mobile_pose_callback(forklift.robot_id),
            fr5_joint_state_qos,
        )

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(
            target=self._executor.spin,
            name="telemetry-gateway-ros-executor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Telemetry ROS subscriber started: FR5/forklift pose BEST_EFFORT, "
            "ZK/Cell status/ready RELIABLE; all VOLATILE."
        )

    def stop(self) -> None:
        if self._executor is None:
            return
        self._executor.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                logger.warning("Telemetry ROS executor thread did not stop within timeout.")
        if self._node is not None:
            self._node.destroy_node()
        self._thread = None
        self._node = None
        self._executor = None
        if self._owns_rclpy_init and self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()
        self._owns_rclpy_init = False

    def _joint_callback(self, robot_id: str) -> Callable[[object], None]:
        def callback(message: object) -> None:
            self._submit(TelemetryUpdate("joint_state", robot_id, normalize_joint_state(message, robot_id=robot_id)))

        return callback

    def _mobile_pose_callback(self, robot_id: str) -> Callable[[object], None]:
        def callback(message: object) -> None:
            payload = normalize_mobile_robot_pose(message, robot_id=robot_id)
            if payload is None:
                logger.warning("Ignoring invalid mobile robot PoseStamped for robot_id=%s.", robot_id)
                return
            self._submit(TelemetryUpdate("mobile_robot_pose", robot_id, payload))

        return callback

    def _status_callback(self, robot_id: str) -> Callable[[object], None]:
        def callback(message: object) -> None:
            raw_data = getattr(message, "data", "")
            payload = normalize_zk_status(raw_data, robot_id=robot_id)
            status_ready = payload.get("ready")
            if isinstance(status_ready, bool):
                self._status_ready[robot_id] = status_ready
                ready_topic = self._ready_topic_value.get(robot_id)
                if ready_topic is not None and ready_topic != status_ready:
                    logger.warning(
                        "%s ready sources disagree: status.ready=%s, /ready=%s; /ready remains authoritative.",
                        robot_id,
                        status_ready,
                        ready_topic,
                    )
            self._submit(TelemetryUpdate("status", robot_id, payload))

        return callback

    def _ready_callback(self, robot_id: str) -> Callable[[object], None]:
        def callback(message: object) -> None:
            ready = bool(getattr(message, "data", False))
            self._ready_topic_value[robot_id] = ready
            status_ready = self._status_ready.get(robot_id)
            if status_ready is not None and status_ready != ready:
                logger.warning(
                    "%s ready sources disagree: status.ready=%s, /ready=%s; /ready remains authoritative.",
                    robot_id,
                    status_ready,
                    ready,
                )
            self._submit(TelemetryUpdate("ready", robot_id, normalize_zk_ready(ready, robot_id=robot_id)))

        return callback

    def _cell_status_callback(self) -> Callable[[object], None]:
        """Observe Cell-status gripper telemetry without affecting FMS commands."""

        def callback(message: object) -> None:
            payload = normalize_fr5_cell_status(getattr(message, "data", ""))
            if payload is None:
                logger.warning("Ignoring invalid /cell/status FR5 telemetry payload.")
                return
            self._submit(TelemetryUpdate("status", payload["robot_id"], payload))

        return callback

    def _submit(self, update: TelemetryUpdate) -> None:
        # ROS callbacks run on the executor thread. ``call_soon_threadsafe`` is
        # the only crossing into the asyncio Redis/handoff loop; no coroutine is
        # awaited and no unbounded queue is created on the ROS side.
        self._loop.call_soon_threadsafe(self._offer_update, update)
