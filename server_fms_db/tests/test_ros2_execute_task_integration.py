"""Opt-in local generated-ExecuteTask integration; no Robot Cell hardware involved."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

if os.getenv("RUN_ROS2_INTEGRATION") != "1":
    pytest.skip("Set RUN_ROS2_INTEGRATION=1 after sourcing factory_ros_ws to run ROS2 integration.", allow_module_level=True)
if os.getenv("ROS_DOMAIN_ID") != "91":
    pytest.skip("ROS2 integration requires isolated ROS_DOMAIN_ID=91.", allow_module_level=True)

import rclpy
from cell_interfaces.action import ExecuteTask
from cell_interfaces.msg import PickCandidate
from cell_interfaces.srv import CellControl, GetPickPose
from rclpy.action import ActionServer, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from fms_server.cell_action_transport import CELL_ACTION_CONTRACT_VERSION, CellTaskCommand, CellTaskOutcome
from fms_server.ros2_cell_action_transport import Ros2CellActionTransport


ACTION_NAME = "/cell/execute_task"
SYNTHETIC_PARTS = '[{"slot":"SYNTH_SLOT_1","class":"furniture","part_code":"SYNTH_PART_1"}]'


@dataclass
class LocalExecuteTaskServer:
    node: Node
    action_server: ActionServer
    executor: MultiThreadedExecutor
    thread: threading.Thread
    received_goals: list[ExecuteTask.Goal] = field(default_factory=list)
    execution_finished: threading.Event = field(default_factory=threading.Event)


@pytest.fixture(scope="module", autouse=True)
def ros_context() -> Iterator[None]:
    rclpy.init(args=None)
    try:
        yield
    finally:
        if rclpy.ok():
            rclpy.shutdown()


@pytest.fixture
def local_execute_task_server() -> Iterator[LocalExecuteTaskServer]:
    node = Node("test_execute_task_action_server")
    received_goals: list[ExecuteTask.Goal] = []

    def goal_callback(goal_request: ExecuteTask.Goal) -> GoalResponse:
        return GoalResponse.REJECT if goal_request.req_id == "ros-e2e-reject" else GoalResponse.ACCEPT

    def execute_callback(goal_handle):
        request = goal_handle.request
        received_goals.append(request)
        if request.req_id == "ros-e2e-timeout":
            time.sleep(0.25)
        feedback = ExecuteTask.Feedback()
        feedback.phase = "MOVE"
        feedback.current_item = 1
        feedback.total_items = 1
        feedback.progress = 1.0
        feedback.robot = "zk2"
        goal_handle.publish_feedback(feedback)
        result = ExecuteTask.Result()
        if request.req_id == "ros-e2e-failed":
            goal_handle.abort()
            result.status = "FAILED"
            result.error_code = "E503"
            result.detail = "synthetic calibration failure"
            result.completed_json = "[]"
        elif request.req_id == "ros-e2e-canceled":
            # The test validates the Cell contract's authoritative Result.status.
            # A ROS cancel transition itself requires a client cancel request, which
            # is deliberately outside the transport's no-auto-cancel timeout policy.
            goal_handle.succeed()
            result.status = "CANCELED"
            result.error_code = ""
            result.detail = "synthetic operator cancel"
            result.completed_json = "[]"
        else:
            goal_handle.succeed()
            result.status = "SUCCEEDED"
            result.error_code = ""
            result.detail = "synthetic success"
            result.completed_json = '["SYNTH_SLOT_1"]'
        server_execution_finished.set()
        return result

    server_execution_finished = threading.Event()
    action_server = ActionServer(
        node, ExecuteTask, ACTION_NAME, execute_callback=execute_callback, goal_callback=goal_callback
    )
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, name="local-execute-task-server", daemon=True)
    thread.start()
    server = LocalExecuteTaskServer(node, action_server, executor, thread, received_goals, server_execution_finished)
    try:
        yield server
    finally:
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        action_server.destroy()
        node.destroy_node()
        assert not thread.is_alive(), "Local ROS2 ActionServer thread leaked."


def _command(*, req_id: str, task_type: str = "INSTALL_FURNITURE") -> CellTaskCommand:
    return CellTaskCommand(
        ver=CELL_ACTION_CONTRACT_VERSION, req_id=req_id, job_id="TEST-JOB", step_id="TEST-STEP",
        task_type=task_type, product="HOUSE_A", parts_json=SYNTHETIC_PARTS,
    )


def _transport(*, result_timeout: float = 1.0) -> Ros2CellActionTransport:
    return Ros2CellActionTransport(
        action_name=ACTION_NAME, server_wait_timeout_seconds=1.0, result_timeout_seconds=result_timeout
    )


def test_generated_success_goal_feedback_and_result_e2e(local_execute_task_server: LocalExecuteTaskServer) -> None:
    accepted: list[bool] = []
    feedback = []
    with _transport() as transport:
        result = transport.execute(
            _command(req_id="ros-e2e-success"),
            goal_accepted_callback=lambda: accepted.append(True),
            feedback_callback=feedback.append,
        )

    assert accepted == [True]
    assert result.outcome is CellTaskOutcome.SUCCESS
    assert result.detail == "synthetic success"
    assert result.completed_slots == ("SYNTH_SLOT_1",)
    assert len(feedback) == 1
    observation = feedback[0]
    assert (observation.phase, observation.current_item, observation.total_items, observation.robot) == (
        "MOVE", 1, 1, "zk2"
    )
    assert observation.progress == pytest.approx(1.0)
    # Result, not progress=1.0, is the completion source.
    received = local_execute_task_server.received_goals[0]
    assert (
        received.ver, received.req_id, received.job_id, received.step_id,
        received.task_type, received.product, received.parts_json,
    ) == (
        "0.2.1", "ros-e2e-success", "TEST-JOB", "TEST-STEP",
        "INSTALL_FURNITURE", "HOUSE_A", SYNTHETIC_PARTS,
    )


def test_generated_auxiliary_interfaces_import_with_expected_fields() -> None:
    assert CellControl.Request.get_fields_and_field_types() == {
        "ver": "string", "cmd": "string", "req_id": "string"
    }
    assert CellControl.Response.get_fields_and_field_types() == {
        "accepted": "boolean", "cell_state": "string", "detail": "string"
    }
    assert {"ver", "req_id", "part_code", "class_name", "zone_id", "max_candidates", "test_mode"} == set(
        GetPickPose.Request.get_fields_and_field_types()
    )
    assert {"pose_valid", "candidates", "error_code", "detail", "calibration_version", "production_valid"} == set(
        GetPickPose.Response.get_fields_and_field_types()
    )
    assert {"candidate_id", "position", "frame_id", "capture_stamp", "pose_valid", "yaw_deg", "yaw_valid", "orientation_source", "quality"} == set(
        PickCandidate.get_fields_and_field_types()
    )


def test_execute_task_goal_version_is_distinct_from_status_and_cell_control_versions() -> None:
    """ExecuteTask v0.2.1 does not redefine the v0.2 status/control channels."""
    assert CELL_ACTION_CONTRACT_VERSION == "0.2.1"
    assert CellControl.Request.get_fields_and_field_types() == {
        "ver": "string", "cmd": "string", "req_id": "string"
    }


def test_wait_for_server_is_goal_free_and_close_remains_clean(
    local_execute_task_server: LocalExecuteTaskServer,
) -> None:
    transport = _transport()
    try:
        assert transport.wait_for_server(timeout_seconds=1.0) is True
        assert local_execute_task_server.received_goals == []
    finally:
        transport.close()
    assert transport.wait_for_server(timeout_seconds=0.0) is False


def test_wait_for_server_returns_false_for_missing_action_server() -> None:
    with Ros2CellActionTransport(
        action_name="/missing_execute_task_action_server",
        server_wait_timeout_seconds=0.05,
    ) as transport:
        assert transport.wait_for_server(timeout_seconds=0.05) is False
def test_generated_interface_configures_material_feed_goal_without_server_dispatch() -> None:
    command = _command(req_id="ros-e2e-material-feed", task_type="MATERIAL_FEED")
    goal = Ros2CellActionTransport._populate_goal(ExecuteTask.Goal(), command)

    assert goal.task_type == "MATERIAL_FEED"
    assert goal.parts_json == SYNTHETIC_PARTS


def test_generated_failed_and_canceled_results_normalize_without_parsing(
    local_execute_task_server: LocalExecuteTaskServer,
) -> None:
    with _transport() as transport:
        failed = transport.execute(_command(req_id="ros-e2e-failed"))
        canceled = transport.execute(_command(req_id="ros-e2e-canceled"))

    assert failed.outcome is CellTaskOutcome.CELL_FAILED
    assert failed.error_code == "E503"
    assert failed.detail == "synthetic calibration failure"
    assert canceled.outcome is CellTaskOutcome.CELL_CANCELED
    assert canceled.detail == "synthetic operator cancel"
    assert canceled.outcome is not CellTaskOutcome.CELL_FAILED


def test_generated_goal_rejection_and_result_timeout_preserve_transport_semantics(
    local_execute_task_server: LocalExecuteTaskServer,
) -> None:
    with _transport(result_timeout=0.05) as transport:
        rejected = transport.execute(_command(req_id="ros-e2e-reject"))
        timed_out = transport.execute(_command(req_id="ros-e2e-timeout"))
        # Let the server finish its intentionally late Result; client cleanup drains
        # this response without issuing a cancel or a redispatch.
        assert local_execute_task_server.execution_finished.wait(timeout=1)
        # Result response delivery occurs immediately after the callback returns.
        time.sleep(0.1)

    assert rejected.outcome is CellTaskOutcome.GOAL_REJECTED
    assert rejected.accepted is False
    assert timed_out.outcome is CellTaskOutcome.RESULT_TIMEOUT
    assert timed_out.accepted is True
