"""Shared persistent ROS 2 ActionClient runtime owned by FMS composition."""

from __future__ import annotations

import uuid
from typing import Any


class Ros2FmsActionRuntimeUnavailableError(RuntimeError):
    """Raised only when an actual ROS transport is selected without its overlay."""


class Ros2FmsActionRuntime:
    """One FMS node/executor shared by Cell and TurtleBot Action clients.

    Imports remain lazy so fake/test collection needs neither rclpy nor generated
    interfaces. The composition root owns this runtime for the FMS process.
    """

    def __init__(self, *, node_name_prefix: str = "fms_action_clients") -> None:
        try:
            import rclpy
            from rclpy.action import ActionClient
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
        except ImportError as exc:
            raise Ros2FmsActionRuntimeUnavailableError(
                "rclpy is unavailable. Source /opt/ros/jazzy/setup.bash and "
                "~/factory_ros_ws/install/setup.bash before selecting CELL_TRANSPORT=ros2."
            ) from exc
        self._rclpy: Any = rclpy
        self._action_client_type: Any = ActionClient
        self._owns_rclpy_init = False
        if not rclpy.ok():
            rclpy.init(args=None)
            self._owns_rclpy_init = True
        self.node: Any = Node(f"{node_name_prefix}_{uuid.uuid4().hex}")
        self.executor: Any = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self._closed = False

    def create_action_client(self, action_type: object, action_name: str) -> object:
        if self._closed:
            raise RuntimeError("ROS action runtime is closed.")
        return self._action_client_type(self.node, action_type, action_name)

    def spin_until_future_complete(self, future: object, timeout_seconds: float) -> bool:
        if self._closed:
            return False
        self.executor.spin_until_future_complete(future, timeout_sec=timeout_seconds)
        return bool(future.done())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.executor.remove_node(self.node)
        except Exception:  # pragma: no cover - defensive ROS cleanup
            pass
        try:
            self.node.destroy_node()
        finally:
            self.executor.shutdown()
        if self._owns_rclpy_init and self._rclpy.ok():
            self._rclpy.shutdown()
