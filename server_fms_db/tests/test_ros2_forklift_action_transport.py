from __future__ import annotations

from dataclasses import dataclass

import pytest

from fms_server.forklift_action_adapter import ForkliftActionStatus
from fms_server.ros2_forklift_action_transport import Ros2ForkliftActionTransport


class ExecuteGoal:
    __slots__ = ("req_id", "job_id", "delivery_id", "pickup_code", "dropoff_code")


class ReturnHomeGoal:
    __slots__ = ("req_id",)


class ExecuteAction:
    Goal = ExecuteGoal


class ReturnHomeAction:
    Goal = ReturnHomeGoal


class Future:
    def __init__(self, value=None, done: bool = True):
        self._value, self._done = value, done
    def done(self): return self._done
    def result(self): return self._value


@dataclass
class Result:
    status: str
    error_code: str = ""
    detail: str = "detail"


class GoalHandle:
    def __init__(self, *, accepted=True, result=None):
        self.accepted = accepted
        self._result = result
    def get_result_async(self):
        return Future(type("Wrapped", (), {"result": self._result})())


class Runtime:
    def spin_until_future_complete(self, future, timeout_seconds):
        return future.done()
    def close(self): pass


class Client:
    def __init__(self, *, available=True, accepted=True, result=None, feedback=None):
        self.available, self.accepted = available, accepted
        self.result, self.feedback = result, feedback
        self.goals = []
    def wait_for_server(self, timeout_sec): return self.available
    def send_goal_async(self, goal, feedback_callback=None):
        self.goals.append(goal)
        if self.feedback is not None and feedback_callback is not None:
            feedback_callback(type("Envelope", (), {"feedback": self.feedback})())
        return Future(GoalHandle(accepted=self.accepted, result=self.result))
    def destroy(self): pass


def _transport(execute: Client | None = None, home: Client | None = None):
    execute, home = execute or Client(result=Result("SUCCEEDED")), home or Client(result=Result("SUCCEEDED"))
    clients = [execute, home]
    return Ros2ForkliftActionTransport(
        execute_action_name="/forklift/execute_transport",
        return_home_action_name="/forklift/return_home",
        runtime=Runtime(), action_client_factory=lambda *_: clients.pop(0),
        execute_action_type=ExecuteAction, return_home_action_type=ReturnHomeAction,
        server_wait_timeout_seconds=0.1, result_timeout_seconds=0.1,
    ), execute, home


def test_execute_transport_goal_maps_exact_five_contract_fields() -> None:
    transport, execute, _ = _transport()
    result = transport.send_execute_transport("req-1", 12, 34, "RACK1", "DROP")
    assert result.status is ForkliftActionStatus.SUCCEEDED
    goal = execute.goals[0]
    assert (goal.req_id, goal.job_id, goal.delivery_id, goal.pickup_code, goal.dropoff_code) == (
        "req-1", 12, 34, "RACK1", "DROP"
    )
    assert ExecuteGoal.__slots__ == ("req_id", "job_id", "delivery_id", "pickup_code", "dropoff_code")


@pytest.mark.parametrize("status, expected", [
    ("SUCCEEDED", ForkliftActionStatus.SUCCEEDED),
    ("FAILED", ForkliftActionStatus.FAILED),
    ("CANCELED", ForkliftActionStatus.CANCELED),
])
def test_terminal_result_status_maps_without_lifecycle_write(status, expected) -> None:
    transport, _, _ = _transport(execute=Client(result=Result(status, "E1", "terminal")))
    result = transport.send_execute_transport("req", 1, 2, "RACK1", "DROP")
    assert result.status is expected
    assert result.error_code == "E1"


def test_feedback_maps_phase_progress_and_detail_unchanged() -> None:
    feedback = type("Feedback", (), {"phase": "DOCKING_PICKUP", "progress": 0.25, "detail": "aligned"})()
    transport, _, _ = _transport(execute=Client(result=Result("SUCCEEDED"), feedback=feedback))
    observed = []
    transport.send_execute_transport("req", 1, 2, "RACK1", "DROP", observed.append)
    assert len(observed) == 1
    assert (observed[0].phase, observed[0].progress, observed[0].detail) == ("DOCKING_PICKUP", 0.25, "aligned")


def test_unavailable_and_rejected_action_servers_fail_closed() -> None:
    unavailable, _, _ = _transport(execute=Client(available=False))
    assert unavailable.send_execute_transport("req", 1, 2, "RACK1", "DROP").error_code == "ACTION_SERVER_UNAVAILABLE"
    rejected, _, _ = _transport(execute=Client(accepted=False))
    assert rejected.send_execute_transport("req", 1, 2, "RACK1", "DROP").error_code == "GOAL_REJECTED"


def test_return_home_goal_contains_only_req_id_and_maps_feedback_result() -> None:
    feedback = type("Feedback", (), {"phase": "RETURNING_HOME", "progress": 0.5, "detail": "corridor"})()
    transport, _, home = _transport(home=Client(result=Result("CANCELED", "", "operator cancel"), feedback=feedback))
    observed = []
    result = transport.send_return_home("home-1", observed.append)
    assert result.status is ForkliftActionStatus.CANCELED
    goal = home.goals[0]
    assert goal.req_id == "home-1"
    assert ReturnHomeGoal.__slots__ == ("req_id",)
    assert (observed[0].phase, observed[0].progress, observed[0].detail) == ("RETURNING_HOME", 0.5, "corridor")


def test_fms_profile_selects_fake_or_shared_ros_transports(monkeypatch) -> None:
    import fms_server.main as main
    from shared.config import Settings

    fake_cell, fake_forklift, fake_runtime = main.create_action_transports(Settings(cell_transport="fake"))
    assert fake_cell.__class__.__name__ == "FakeCellActionTransport"
    assert fake_forklift.__class__.__name__ == "FakeForkliftActionTransport"
    assert fake_runtime is None

    class SharedRuntime:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    class ActualCell:
        def __init__(self, **kwargs): self.runtime = kwargs["runtime"]
    class ActualForklift:
        def __init__(self, **kwargs): self.runtime = kwargs["runtime"]

    import fms_server.ros2_fms_action_runtime as runtime_module
    import fms_server.ros2_cell_action_transport as cell_module
    import fms_server.ros2_forklift_action_transport as forklift_module
    monkeypatch.setattr(runtime_module, "Ros2FmsActionRuntime", SharedRuntime)
    monkeypatch.setattr(cell_module, "Ros2CellActionTransport", ActualCell)
    monkeypatch.setattr(forklift_module, "Ros2ForkliftActionTransport", ActualForklift)
    actual_cell, actual_forklift, runtime = main.create_action_transports(Settings(cell_transport="ros2"))
    assert isinstance(actual_cell, ActualCell) and isinstance(actual_forklift, ActualForklift)
    assert actual_cell.runtime is actual_forklift.runtime is runtime
