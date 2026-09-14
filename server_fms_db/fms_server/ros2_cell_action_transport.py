"""Lazy ROS 2 ActionClient transport for the generated ExecuteTask interface.

The generated ``cell_interfaces`` overlay remains external to Backend.  Imports
are deferred so ordinary FastAPI and non-ROS test collection never require ROS.
"""

from __future__ import annotations

from typing import Any

from fms_server.ros2_fms_action_runtime import Ros2FmsActionRuntime

from fms_server.cell_action_transport import (
    CELL_ACTION_NAME,
    CellTaskAcceptedCallback,
    CellTaskCommand,
    CellTaskExecutionResult,
    CellTaskFeedbackCallback,
)
from shared.config import get_settings


class CellActionContractUnavailableError(RuntimeError):
    """The ROS overlay lacks an executable verified ExecuteTask ActionClient."""


class Ros2CellActionTransport:
    """Synchronous Cell Action transport using an optional shared FMS runtime.

    It contacts only the configured action name in the current ROS domain. The
    caller remains responsible for configuring the intended ROS domain; this
    adapter does not select or override ``ROS_DOMAIN_ID``.
    """

    def __init__(
        self,
        *,
        action_name: str | None = None,
        server_wait_timeout_seconds: float | None = None,
        result_timeout_seconds: float | None = None,
        runtime: Ros2FmsActionRuntime | None = None,
    ) -> None:
        settings = get_settings()
        self.action_name = action_name or settings.cell_execute_task_action_name or CELL_ACTION_NAME
        self.server_wait_timeout_seconds = (
            server_wait_timeout_seconds
            if server_wait_timeout_seconds is not None
            else settings.cell_action_server_wait_timeout_seconds
        )
        self.result_timeout_seconds = (
            result_timeout_seconds
            if result_timeout_seconds is not None
            else settings.cell_action_result_timeout_seconds
        )
        try:
            from cell_interfaces.action import ExecuteTask
        except ImportError as exc:
            raise CellActionContractUnavailableError(
                "cell_interfaces.action.ExecuteTask is unavailable. Source /opt/ros/jazzy/setup.bash "
                "and ~/factory_ros_ws/install/setup.bash before constructing Ros2CellActionTransport."
            ) from exc

        self._execute_task: Any = ExecuteTask
        self._runtime = runtime or Ros2FmsActionRuntime(node_name_prefix="fms_execute_task_client")
        self._owns_runtime = runtime is None
        self._client: Any = self._runtime.create_action_client(ExecuteTask, self.action_name)
        self._pending_result_futures: list[object] = []
        self._closed = False

    def close(self) -> None:
        """Release this transport's Node/Executor without shutting down shared ROS.

        A transport initializes rclpy only when no caller did; in that case it also
        owns the matching shutdown.  Test ActionServers that initialized rclpy
        themselves remain untouched.
        """

        if self._closed:
            return
        # A timeout never auto-cancels or retries a Cell Goal.  During explicit
        # client teardown only, briefly drain already-returned result futures so
        # rclpy can release Action protocol entities cleanly.
        for future in self._pending_result_futures:
            if not future.done():
                try:
                    self._spin_until(future, 0.5)
                except Exception:  # pragma: no cover - cleanup must stay best effort
                    pass
        self._pending_result_futures.clear()
        self._closed = True
        try:
            self._client.destroy()
        except Exception:  # pragma: no cover - rclpy version dependent
            pass
        if self._owns_runtime:
            self._runtime.close()

    def __enter__(self) -> "Ros2CellActionTransport":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def wait_for_server(self, timeout_seconds: float | None = None) -> bool:
        """Probe ActionServer availability without constructing or sending a Goal.

        This is an explicit preflight boundary for operators. It reuses this
        transport's existing ActionClient and FMS Action runtime lifecycle.
        """

        if self._closed:
            return False
        timeout = self.server_wait_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("timeout_seconds must be non-negative.")
        try:
            return bool(self._client.wait_for_server(timeout_sec=timeout))
        except Exception:
            return False

    @staticmethod
    def _populate_goal(goal: object, command: CellTaskCommand) -> object:
        """Populate only the seven verified generated Goal fields."""
        goal.ver = command.ver
        goal.req_id = command.req_id
        goal.job_id = command.job_id
        goal.step_id = command.step_id
        goal.task_type = command.task_type
        goal.product = command.product
        goal.parts_json = command.parts_json
        return goal

    @staticmethod
    def _normalize_generated_result(result: object) -> CellTaskExecutionResult:
        """Read exactly the verified generated Result fields."""
        from fms_server.cell_action_transport import CellTaskRawResult

        return CellTaskRawResult(
            status=result.status,
            error_code=result.error_code,
            detail=result.detail,
            completed_json=result.completed_json,
        ).normalize()

    @staticmethod
    def _normalize_generated_feedback(command: CellTaskCommand, feedback: object):
        """Read exactly the verified generated Feedback fields."""
        from fms_server.cell_action_transport import CellTaskFeedback, CellTaskRawFeedback

        return CellTaskFeedback.from_raw(
            command=command,
            raw=CellTaskRawFeedback(
                phase=feedback.phase,
                current_item=feedback.current_item,
                total_items=feedback.total_items,
                progress=feedback.progress,
                robot=feedback.robot,
            ),
        )

    def execute(
        self,
        command: CellTaskCommand,
        *,
        goal_accepted_callback: CellTaskAcceptedCallback | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> CellTaskExecutionResult:
        if self._closed:
            return CellTaskExecutionResult.transport_error(detail="Ros2CellActionTransport is closed.")
        try:
            if not self._client.wait_for_server(timeout_sec=self.server_wait_timeout_seconds):
                return CellTaskExecutionResult.server_unavailable(
                    detail=f"ExecuteTask ActionServer unavailable: {self.action_name}."
                )
            goal = self._populate_goal(self._execute_task.Goal(), command)

            def on_feedback(feedback_message: object) -> None:
                if feedback_callback is not None:
                    feedback_callback(
                        self._normalize_generated_feedback(command, feedback_message.feedback)
                    )

            goal_future = self._client.send_goal_async(goal, feedback_callback=on_feedback)
            if not self._spin_until(goal_future, self.server_wait_timeout_seconds):
                return CellTaskExecutionResult.transport_error(
                    detail="Timed out waiting for ExecuteTask goal acceptance response.", accepted=False
                )
            goal_handle = goal_future.result()
            if goal_handle is None or not goal_handle.accepted:
                return CellTaskExecutionResult.goal_rejected(
                    detail="ExecuteTask ActionServer rejected the Goal."
                )
            if goal_accepted_callback is not None:
                goal_accepted_callback()
            result_future = goal_handle.get_result_async()
            if not self._spin_until(result_future, self.result_timeout_seconds):
                self._pending_result_futures.append(result_future)
                return CellTaskExecutionResult.result_timeout(
                    detail="Timed out waiting for ExecuteTask Action Result."
                )
            wrapped_result = result_future.result()
            if wrapped_result is None:
                return CellTaskExecutionResult.transport_error(
                    detail="ExecuteTask Action Result future completed without a result.", accepted=True
                )
            return self._normalize_generated_result(wrapped_result.result)
        except Exception as exc:
            return CellTaskExecutionResult.transport_error(detail=f"ExecuteTask ROS transport error: {exc}", accepted=False)

    def _spin_until(self, future: object, timeout_seconds: float) -> bool:
        return self._runtime.spin_until_future_complete(future, timeout_seconds)
