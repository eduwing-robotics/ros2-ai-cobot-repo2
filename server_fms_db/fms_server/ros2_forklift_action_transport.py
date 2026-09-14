"""Lazy ROS2 Action transport for the frozen TurtleBot logical contract."""

from __future__ import annotations

from typing import Any, Callable

from fms_server.forklift_action_adapter import (
    ForkliftActionStatus,
    ForkliftExecutionFeedback,
    ForkliftExecutionResult,
    ForkliftFeedbackCallback,
)
from fms_server.ros2_fms_action_runtime import Ros2FmsActionRuntime
from shared.config import get_settings


class ForkliftActionContractUnavailableError(RuntimeError):
    """The actual generated forklift Action interfaces are unavailable."""


class Ros2ForkliftActionTransport:
    """Persistent ActionClient adapter; it never mutates Delivery state directly."""

    def __init__(
        self,
        *,
        execute_action_name: str | None = None,
        return_home_action_name: str | None = None,
        server_wait_timeout_seconds: float | None = None,
        result_timeout_seconds: float | None = None,
        runtime: Ros2FmsActionRuntime | None = None,
        action_client_factory: Callable[[object, str], object] | None = None,
        execute_action_type: object | None = None,
        return_home_action_type: object | None = None,
    ) -> None:
        settings = get_settings()
        self.execute_action_name = execute_action_name or settings.forklift_execute_transport_action_name
        self.return_home_action_name = return_home_action_name or settings.forklift_return_home_action_name
        self.server_wait_timeout_seconds = (
            settings.cell_action_server_wait_timeout_seconds
            if server_wait_timeout_seconds is None else server_wait_timeout_seconds
        )
        self.result_timeout_seconds = (
            settings.cell_action_result_timeout_seconds
            if result_timeout_seconds is None else result_timeout_seconds
        )
        if self.server_wait_timeout_seconds <= 0 or self.result_timeout_seconds <= 0:
            raise ValueError("ROS Action timeouts must be positive.")
        if execute_action_type is None or return_home_action_type is None:
            try:
                from forklift_interfaces.action import ExecuteTransport, ReturnHome
            except ImportError as exc:
                raise ForkliftActionContractUnavailableError(
                    "forklift_interfaces.action.ExecuteTransport/ReturnHome are unavailable. "
                    "Source ~/factory_ros_ws/install/setup.bash before selecting CELL_TRANSPORT=ros2."
                ) from exc
            execute_action_type = execute_action_type or ExecuteTransport
            return_home_action_type = return_home_action_type or ReturnHome
        self._runtime = runtime or Ros2FmsActionRuntime()
        self._owns_runtime = runtime is None
        factory = action_client_factory or self._runtime.create_action_client
        self._execute_action_type: Any = execute_action_type
        self._return_home_action_type: Any = return_home_action_type
        self._execute_client: Any = factory(execute_action_type, self.execute_action_name)
        self._return_home_client: Any = factory(return_home_action_type, self.return_home_action_name)
        self._pending_result_futures: list[object] = []
        self._closed = False

    @staticmethod
    def _execute_goal(action_type: object, *, req_id: str, job_id: int, delivery_id: int,
                      pickup_code: str, dropoff_code: str) -> object:
        goal = action_type.Goal()
        goal.req_id = req_id
        goal.job_id = job_id
        goal.delivery_id = delivery_id
        goal.pickup_code = pickup_code
        goal.dropoff_code = dropoff_code
        return goal

    @staticmethod
    def _return_home_goal(action_type: object, *, req_id: str) -> object:
        goal = action_type.Goal()
        goal.req_id = req_id
        return goal

    @staticmethod
    def _result_from_generated(result: object) -> ForkliftExecutionResult:
        raw_status = getattr(result, "status", None)
        try:
            status = ForkliftActionStatus(raw_status)
        except (TypeError, ValueError):
            return ForkliftExecutionResult(
                status=ForkliftActionStatus.FAILED,
                error_code="INVALID_ACTION_RESULT",
                detail=f"Unsupported forklift Action Result status: {raw_status!r}",
            )
        error_code = getattr(result, "error_code", "")
        detail = getattr(result, "detail", "")
        if not isinstance(error_code, str) or not isinstance(detail, str):
            return ForkliftExecutionResult(
                status=ForkliftActionStatus.FAILED,
                error_code="INVALID_ACTION_RESULT",
                detail="Forklift Action Result error_code/detail must be strings.",
            )
        return ForkliftExecutionResult(status=status, error_code=error_code, detail=detail)

    @staticmethod
    def _feedback_from_generated(message: object) -> ForkliftExecutionFeedback:
        return ForkliftExecutionFeedback(
            phase=getattr(message, "phase"),
            progress=getattr(message, "progress"),
            detail=getattr(message, "detail"),
        )

    def send_execute_transport(self, req_id: str, job_id: int, delivery_id: int,
                               pickup_code: str, dropoff_code: str,
                               feedback_callback: ForkliftFeedbackCallback | None = None) -> ForkliftExecutionResult:
        goal = self._execute_goal(
            self._execute_action_type, req_id=req_id, job_id=job_id,
            delivery_id=delivery_id, pickup_code=pickup_code, dropoff_code=dropoff_code,
        )
        return self._send(
            client=self._execute_client, action_name=self.execute_action_name, goal=goal,
            feedback_callback=feedback_callback,
        )

    def send_return_home(self, req_id: str,
                         feedback_callback: ForkliftFeedbackCallback | None = None) -> ForkliftExecutionResult:
        goal = self._return_home_goal(self._return_home_action_type, req_id=req_id)
        return self._send(
            client=self._return_home_client, action_name=self.return_home_action_name, goal=goal,
            feedback_callback=feedback_callback,
        )

    def _send(self, *, client: object, action_name: str, goal: object,
              feedback_callback: ForkliftFeedbackCallback | None) -> ForkliftExecutionResult:
        if self._closed:
            return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "TRANSPORT_CLOSED", "ROS forklift transport is closed.")
        try:
            if not client.wait_for_server(timeout_sec=self.server_wait_timeout_seconds):
                return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "ACTION_SERVER_UNAVAILABLE", f"ActionServer unavailable: {action_name}.")

            def on_feedback(message: object) -> None:
                if feedback_callback is not None:
                    feedback_callback(self._feedback_from_generated(message.feedback))

            goal_future = client.send_goal_async(goal, feedback_callback=on_feedback)
            if not self._runtime.spin_until_future_complete(goal_future, self.server_wait_timeout_seconds):
                return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "GOAL_ACCEPT_TIMEOUT", f"Timed out waiting for goal acceptance: {action_name}.")
            goal_handle = goal_future.result()
            if goal_handle is None or not goal_handle.accepted:
                return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "GOAL_REJECTED", f"ActionServer rejected Goal: {action_name}.")
            result_future = goal_handle.get_result_async()
            if not self._runtime.spin_until_future_complete(result_future, self.result_timeout_seconds):
                self._pending_result_futures.append(result_future)
                return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "RESULT_TIMEOUT", f"Timed out waiting for Action Result: {action_name}.")
            wrapped = result_future.result()
            if wrapped is None:
                return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "RESULT_EXCEPTION", "Action Result future completed without a result.")
            return self._result_from_generated(wrapped.result)
        except Exception as exc:
            return ForkliftExecutionResult(ForkliftActionStatus.FAILED, "ROS_ACTION_ERROR", f"ROS Action transport error: {exc}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in (self._execute_client, self._return_home_client):
            try:
                client.destroy()
            except Exception:  # pragma: no cover - rclpy version dependent cleanup
                pass
        if self._owns_runtime:
            self._runtime.close()

    def __enter__(self) -> "Ros2ForkliftActionTransport":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
