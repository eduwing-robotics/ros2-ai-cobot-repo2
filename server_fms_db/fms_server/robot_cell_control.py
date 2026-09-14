"""FMS-owned Robot Cell control boundary.

CellControl ACKs are never physical completion proof. Pause/resume settlement is
owned by the coordinator and correlated /cell/status telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from fms_server.ros2_fms_action_runtime import Ros2FmsActionRuntime
from shared.config import get_settings


CELL_CONTROL_NAME = "/cell/control"
CELL_CONTROL_CONTRACT_VERSION = "0.3"
CELL_CONTROL_LEGACY_CONTRACT_VERSION = "0.2"


class RobotCellControlCommand(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    ABORT = "ABORT"
    RESET = "RESET"


@dataclass(frozen=True, slots=True)
class RobotCellControlResult:
    accepted: bool
    cell_state: str | None
    detail: str | None = None
    stop_mode: str | None = None
    eta_ms: int | None = None


def normalize_eta_ms(value: object) -> int | None:
    """Normalize the v0.3 transitional string/int ETA without making it authority."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdecimal():
            return int(text)
    return None


class RobotCellControlPort(Protocol):
    def control(
        self,
        *,
        command: RobotCellControlCommand,
        req_id: str,
        immediate: bool = False,
    ) -> RobotCellControlResult:
        """Submit a control ACK request; this is not physical completion proof."""


class FakeRobotCellControlPort:
    """Recording fake used only by isolated tests and fake FMS composition."""

    def __init__(self, result: RobotCellControlResult | None = None) -> None:
        self.requests: list[tuple[RobotCellControlCommand, str]] = []
        self.request_immediates: list[bool] = []
        self.result = result or RobotCellControlResult(True, "EXECUTE", "fake accepted")

    def control(
        self,
        *,
        command: RobotCellControlCommand,
        req_id: str,
        immediate: bool = False,
    ) -> RobotCellControlResult:
        self.requests.append((command, req_id))
        self.request_immediates.append(bool(immediate) if command is RobotCellControlCommand.PAUSE else False)
        return self.result


class Ros2RobotCellControlPort:
    """Lazy synchronous CellControl client supporting generated v0.2 and v0.3 types."""

    def __init__(
        self,
        *,
        service_name: str = CELL_CONTROL_NAME,
        timeout_seconds: float | None = None,
        runtime: Ros2FmsActionRuntime | None = None,
        service_type: object | None = None,
    ) -> None:
        settings = get_settings()
        self._service_name = service_name
        self._timeout = timeout_seconds if timeout_seconds is not None else settings.cell_action_server_wait_timeout_seconds
        if self._timeout <= 0:
            raise ValueError("Cell control timeout must be positive.")
        if service_type is None:
            try:
                from cell_interfaces.srv import CellControl
            except ImportError as exc:
                raise RuntimeError("cell_interfaces.srv.CellControl is unavailable.") from exc
            service_type = CellControl
        self._service_type: Any = service_type
        self._runtime = runtime or Ros2FmsActionRuntime(node_name_prefix="fms_cell_control_client")
        self._owns_runtime = runtime is None
        self._client: Any = self._runtime.node.create_client(service_type, service_name)
        self._closed = False

    def control(
        self,
        *,
        command: RobotCellControlCommand,
        req_id: str,
        immediate: bool = False,
    ) -> RobotCellControlResult:
        if self._closed:
            return RobotCellControlResult(False, None, "Robot Cell control client is closed.")
        try:
            if not self._client.wait_for_service(timeout_sec=self._timeout):
                return RobotCellControlResult(False, None, f"CellControl unavailable: {self._service_name}.")
            request = self._service_type.Request()
            # Existing generated v0.2 interfaces have no immediate member. They
            # remain compatible until deployment updates the generated service.
            supports_v03 = hasattr(request, "immediate")
            request.ver = CELL_CONTROL_CONTRACT_VERSION if supports_v03 else CELL_CONTROL_LEGACY_CONTRACT_VERSION
            request.cmd = command.value
            request.req_id = req_id
            if supports_v03:
                request.immediate = bool(immediate) if command is RobotCellControlCommand.PAUSE else False
            future = self._client.call_async(request)
            if not self._runtime.spin_until_future_complete(future, self._timeout):
                return RobotCellControlResult(False, None, "Timed out waiting for CellControl response.")
            response = future.result()
            if response is None:
                return RobotCellControlResult(False, None, "CellControl response was empty.")
            return RobotCellControlResult(
                bool(getattr(response, "accepted", False)),
                response.cell_state if isinstance(getattr(response, "cell_state", None), str) else None,
                response.detail if isinstance(getattr(response, "detail", None), str) else None,
                response.stop_mode if isinstance(getattr(response, "stop_mode", None), str) else None,
                normalize_eta_ms(getattr(response, "eta_ms", None)),
            )
        except Exception as exc:
            return RobotCellControlResult(False, None, f"CellControl transport error: {exc}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.destroy()
        except Exception:  # pragma: no cover - rclpy version dependent
            pass
        if self._owns_runtime:
            self._runtime.close()
