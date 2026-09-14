from __future__ import annotations

from types import SimpleNamespace

import pytest

from fms_server.robot_cell_control import (
    CELL_CONTROL_CONTRACT_VERSION,
    CELL_CONTROL_LEGACY_CONTRACT_VERSION,
    RobotCellControlCommand,
    Ros2RobotCellControlPort,
    normalize_eta_ms,
)


class _Future:
    def __init__(self, response) -> None:
        self._response = response

    def result(self):
        return self._response


class _Client:
    def __init__(self, response) -> None:
        self.response = response
        self.requests = []

    def wait_for_service(self, *, timeout_sec: float) -> bool:
        return True

    def call_async(self, request):
        self.requests.append(request)
        return _Future(self.response)

    def destroy(self) -> None:
        pass


class _Runtime:
    def __init__(self, client: _Client) -> None:
        self.node = SimpleNamespace(create_client=lambda _type, _name: client)

    def spin_until_future_complete(self, future, timeout: float) -> bool:
        return True

    def close(self) -> None:
        pass


class _V03Request:
    ver = ""
    cmd = ""
    req_id = ""
    immediate = False


class _V02Request:
    ver = ""
    cmd = ""
    req_id = ""


class _V03Service:
    Request = _V03Request


class _V02Service:
    Request = _V02Request


def _port(service_type, response):
    client = _Client(response)
    return Ros2RobotCellControlPort(
        service_type=service_type, runtime=_Runtime(client), timeout_seconds=1,
    ), client


def test_v03_pause_writes_immediate_and_retains_ack_metadata() -> None:
    response = SimpleNamespace(
        accepted=True, cell_state="EXECUTE", detail="accepted", stop_mode="DEFERRED_UNSAFE", eta_ms="2500"
    )
    port, client = _port(_V03Service, response)
    result = port.control(command=RobotCellControlCommand.PAUSE, req_id="pause-1", immediate=True)
    request = client.requests[0]
    assert (request.ver, request.cmd, request.req_id, request.immediate) == (
        CELL_CONTROL_CONTRACT_VERSION, "PAUSE", "pause-1", True
    )
    assert (result.accepted, result.cell_state, result.detail, result.stop_mode, result.eta_ms) == (
        True, "EXECUTE", "accepted", "DEFERRED_UNSAFE", 2500
    )


def test_v03_eta_integer_and_malformed_values_are_safe() -> None:
    response = SimpleNamespace(
        accepted=True, cell_state="EXECUTE", detail=None, stop_mode="IMMEDIATE", eta_ms=42
    )
    port, _ = _port(_V03Service, response)
    assert port.control(command=RobotCellControlCommand.PAUSE, req_id="pause-2").eta_ms == 42
    assert normalize_eta_ms("bad") is None
    assert normalize_eta_ms(-1) is None
    assert normalize_eta_ms(True) is None


def test_v02_generated_interface_remains_compatible() -> None:
    response = SimpleNamespace(accepted=True, cell_state="EXECUTE", detail="legacy")
    port, client = _port(_V02Service, response)
    result = port.control(command=RobotCellControlCommand.PAUSE, req_id="legacy", immediate=True)
    request = client.requests[0]
    assert (request.ver, request.cmd, request.req_id) == (CELL_CONTROL_LEGACY_CONTRACT_VERSION, "PAUSE", "legacy")
    assert not hasattr(request, "immediate")
    assert result.stop_mode is None and result.eta_ms is None


def test_resume_never_sets_immediate_even_with_v03_request() -> None:
    response = SimpleNamespace(accepted=True, cell_state="HELD", detail=None, stop_mode="NOT_APPLICABLE", eta_ms="0")
    port, client = _port(_V03Service, response)
    port.control(command=RobotCellControlCommand.RESUME, req_id="resume-1", immediate=True)
    assert client.requests[0].immediate is False
