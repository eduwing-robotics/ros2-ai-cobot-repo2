"""Map logical material deliveries to the Forklift execute_transport and return_home ROS2 actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)


class ForkliftAdapterError(RuntimeError):
    """Base error for Forklift FMS-to-Cell mappings."""


class ForkliftActionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ExecuteTransportPhase(StrEnum):
    MOVING_TO_PICKUP = "MOVING_TO_PICKUP"
    DOCKING_PICKUP = "DOCKING_PICKUP"
    LIFTING_UP = "LIFTING_UP"
    MOVING_TO_DROPOFF = "MOVING_TO_DROPOFF"
    DOCKING_DROPOFF = "DOCKING_DROPOFF"
    LIFTING_DOWN = "LIFTING_DOWN"


class ReturnHomePhase(StrEnum):
    RETURNING_HOME = "RETURNING_HOME"


@dataclass(frozen=True)
class ForkliftExecutionResult:
    status: ForkliftActionStatus
    error_code: str
    detail: str


@dataclass(frozen=True)
class ForkliftExecutionFeedback:
    """Raw feedback received from a Forklift Action execution."""

    phase: str
    progress: float
    detail: str


ForkliftFeedbackCallback = Callable[[ForkliftExecutionFeedback], None]


class ForkliftActionTransport:
    """Abstract interface for sending actions to the Forklift."""
    def send_execute_transport(
        self,
        req_id: str,
        job_id: int,
        delivery_id: int,
        pickup_code: str,
        dropoff_code: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        raise NotImplementedError

    def send_return_home(
        self,
        req_id: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        raise NotImplementedError


class FakeForkliftActionTransport(ForkliftActionTransport):
    """Configurable fake transport for unit tests."""

    def __init__(
        self,
        *,
        execute_transport_feedback: Sequence[ForkliftExecutionFeedback] = (),
        return_home_feedback: Sequence[ForkliftExecutionFeedback] = (),
        execute_transport_result: ForkliftExecutionResult | None = None,
        return_home_result: ForkliftExecutionResult | None = None,
    ) -> None:
        self.execute_transport_requests: list[dict[str, Any]] = []
        self.return_home_requests: list[dict[str, Any]] = []
        self._execute_transport_feedback = tuple(execute_transport_feedback)
        self._return_home_feedback = tuple(return_home_feedback)
        self._execute_transport_result = execute_transport_result or ForkliftExecutionResult(
            status=ForkliftActionStatus.SUCCEEDED,
            error_code="",
            detail="Fake execution succeeded",
        )
        self._return_home_result = return_home_result or ForkliftExecutionResult(
            status=ForkliftActionStatus.SUCCEEDED,
            error_code="",
            detail="Fake return home succeeded",
        )

    def send_execute_transport(
        self,
        req_id: str,
        job_id: int,
        delivery_id: int,
        pickup_code: str,
        dropoff_code: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        self.execute_transport_requests.append({
            "req_id": req_id,
            "job_id": job_id,
            "delivery_id": delivery_id,
            "pickup_code": pickup_code,
            "dropoff_code": dropoff_code,
        })
        if feedback_callback is not None:
            for feedback in self._execute_transport_feedback:
                feedback_callback(feedback)
        return self._execute_transport_result

    def send_return_home(
        self,
        req_id: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        self.return_home_requests.append({"req_id": req_id})
        if feedback_callback is not None:
            for feedback in self._return_home_feedback:
                feedback_callback(feedback)
        return self._return_home_result


class ForkliftActionAdapter:
    """FMS side adapter formatting logical goals for the Forklift action contract."""

    def __init__(self, transport: ForkliftActionTransport) -> None:
        self._transport = transport

    def dispatch_execute_transport(
        self,
        req_id: str,
        job_id: int,
        delivery_id: int,
        pickup_code: str,
        dropoff_code: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        """
        job_id, delivery_id must be integers according to the latest contract.
        req_id is string.
        """
        if not isinstance(job_id, int):
            raise ForkliftAdapterError(f"job_id must be int, got {type(job_id)}")
        if not isinstance(delivery_id, int):
            raise ForkliftAdapterError(f"delivery_id must be int, got {type(delivery_id)}")
        if not isinstance(pickup_code, str) or not pickup_code.strip():
            raise ForkliftAdapterError("pickup_code must be a non-empty string.")
        if not isinstance(dropoff_code, str) or not dropoff_code.strip():
            raise ForkliftAdapterError("dropoff_code must be a non-empty string.")

        return self._transport.send_execute_transport(
            req_id=req_id,
            job_id=job_id,
            delivery_id=delivery_id,
            pickup_code=pickup_code,
            dropoff_code=dropoff_code,
            feedback_callback=feedback_callback,
        )

    def dispatch_return_home(
        self,
        req_id: str,
        feedback_callback: ForkliftFeedbackCallback | None = None,
    ) -> ForkliftExecutionResult:
        return self._transport.send_return_home(
            req_id=req_id,
            feedback_callback=feedback_callback,
        )
