"""Transport-neutral contract for the verified Robot Cell ExecuteTask Action."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol


CELL_ACTION_NAME = "/cell/execute_task"
# This is the ExecuteTask Goal contract version only.  It is intentionally
# independent from /cell/status and CellControl, which currently use "0.2".
CELL_ACTION_CONTRACT_VERSION = "0.2.1"

CELL_FEEDBACK_PHASES = frozenset({
    "OBSERVE", "PICK", "REORIENT", "PLACE_APPROACH", "INSERT", "RETREAT", "MOVE", "SUCTION", "RELEASE",
})
CELL_FEEDBACK_ROBOTS = frozenset({"fr5", "zk1", "zk2"})


class CellTaskOutcome(StrEnum):
    """Normalized Action outcomes; transport failures are not Cell failures."""

    SUCCESS = "SUCCESS"
    GOAL_REJECTED = "GOAL_REJECTED"
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    RESULT_TIMEOUT = "RESULT_TIMEOUT"
    CELL_FAILED = "CELL_FAILED"
    CELL_CANCELED = "CELL_CANCELED"
    ROS_TRANSPORT_ERROR = "ROS_TRANSPORT_ERROR"


class CellTaskResultNormalizationError(ValueError):
    """The Robot Cell returned a terminal Result outside the verified contract."""


class CellTaskFeedbackValidationError(ValueError):
    """The Robot Cell returned Feedback outside the verified contract."""


@dataclass(frozen=True, slots=True)
class CellTaskCommand:
    """Exact logical ExecuteTask Goal: the seven verified top-level fields only."""

    ver: str
    req_id: str
    job_id: str
    step_id: str
    task_type: str
    product: str
    parts_json: str


@dataclass(frozen=True, slots=True)
class CellTaskRawResult:
    """Raw ExecuteTask Result fields before FMS outcome normalization."""

    status: str
    error_code: str = ""
    detail: str = ""
    completed_json: str = "[]"

    def normalize(self) -> "CellTaskExecutionResult":
        status = self.status.strip().upper() if isinstance(self.status, str) else ""
        error_code = self.error_code.strip() if isinstance(self.error_code, str) else ""
        detail = self.detail.strip() if isinstance(self.detail, str) else ""
        completed_slots = _parse_completed_json(self.completed_json)
        if status == "SUCCEEDED":
            return CellTaskExecutionResult(
                outcome=CellTaskOutcome.SUCCESS,
                accepted=True,
                succeeded=True,
                detail=detail or None,
                completed_slots=completed_slots,
                raw_result=self,
            )
        if status == "FAILED":
            return CellTaskExecutionResult(
                outcome=CellTaskOutcome.CELL_FAILED,
                accepted=True,
                succeeded=False,
                error_code=error_code or None,
                detail=detail or None,
                completed_slots=completed_slots,
                raw_result=self,
            )
        if status == "CANCELED":
            return CellTaskExecutionResult(
                outcome=CellTaskOutcome.CELL_CANCELED,
                accepted=True,
                succeeded=None,
                error_code=error_code or None,
                detail=detail or None,
                completed_slots=completed_slots,
                raw_result=self,
            )
        raise CellTaskResultNormalizationError(
            "ExecuteTask Result.status must be one of SUCCEEDED, FAILED, or CANCELED."
        )


@dataclass(frozen=True, slots=True)
class CellTaskRawFeedback:
    """Raw ExecuteTask Feedback fields before adding FMS dispatch context."""

    phase: str
    current_item: int
    total_items: int
    progress: float
    robot: str

    def validate(self) -> "CellTaskRawFeedback":
        if self.phase not in CELL_FEEDBACK_PHASES:
            raise CellTaskFeedbackValidationError(f"Unsupported ExecuteTask Feedback.phase: {self.phase!r}.")
        if not isinstance(self.current_item, int) or isinstance(self.current_item, bool) or self.current_item < 0:
            raise CellTaskFeedbackValidationError("Feedback.current_item must be a non-negative uint32 value.")
        if not isinstance(self.total_items, int) or isinstance(self.total_items, bool) or self.total_items < 0:
            raise CellTaskFeedbackValidationError("Feedback.total_items must be a non-negative uint32 value.")
        if not isinstance(self.progress, (int, float)) or not 0.0 <= float(self.progress) <= 1.0:
            raise CellTaskFeedbackValidationError("Feedback.progress must be within 0.0 through 1.0.")
        if self.robot not in CELL_FEEDBACK_ROBOTS:
            raise CellTaskFeedbackValidationError(f"Unsupported ExecuteTask Feedback.robot: {self.robot!r}.")
        return self


@dataclass(frozen=True, slots=True)
class CellTaskFeedback:
    """Verified Feedback fields plus immutable FMS request context."""

    req_id: str
    job_id: str
    step_id: str
    phase: str
    current_item: int
    total_items: int
    progress: float
    robot: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_raw(
        cls, *, command: CellTaskCommand, raw: CellTaskRawFeedback
    ) -> "CellTaskFeedback":
        checked = raw.validate()
        return cls(
            req_id=command.req_id,
            job_id=command.job_id,
            step_id=command.step_id,
            phase=checked.phase,
            current_item=checked.current_item,
            total_items=checked.total_items,
            progress=float(checked.progress),
            robot=checked.robot,
        )


@dataclass(frozen=True, slots=True)
class CellTaskExecutionResult:
    """Normalized terminal Action observation preserving verified Result data."""

    outcome: CellTaskOutcome
    accepted: bool
    succeeded: bool | None
    error_code: str | None = None
    detail: str | None = None
    completed_slots: tuple[str, ...] = ()
    raw_result: object | None = None

    @property
    def authoritative(self) -> bool:
        return self.outcome in {
            CellTaskOutcome.SUCCESS,
            CellTaskOutcome.CELL_FAILED,
            CellTaskOutcome.CELL_CANCELED,
        }

    @classmethod
    def from_raw_result(cls, raw: CellTaskRawResult) -> "CellTaskExecutionResult":
        return raw.normalize()

    @classmethod
    def success(cls, *, detail: str | None = None, completed_slots: tuple[str, ...] = ()) -> "CellTaskExecutionResult":
        return CellTaskRawResult(
            status="SUCCEEDED", detail=detail or "", completed_json=json.dumps(list(completed_slots))
        ).normalize()

    @classmethod
    def cell_failed(
        cls, *, error_code: str | None, detail: str | None = None, completed_slots: tuple[str, ...] = ()
    ) -> "CellTaskExecutionResult":
        return CellTaskRawResult(
            status="FAILED", error_code=error_code or "", detail=detail or "", completed_json=json.dumps(list(completed_slots))
        ).normalize()

    @classmethod
    def cell_canceled(
        cls, *, error_code: str | None = None, detail: str | None = None, completed_slots: tuple[str, ...] = ()
    ) -> "CellTaskExecutionResult":
        return CellTaskRawResult(
            status="CANCELED", error_code=error_code or "", detail=detail or "", completed_json=json.dumps(list(completed_slots))
        ).normalize()

    @classmethod
    def goal_rejected(cls, *, detail: str | None = None) -> "CellTaskExecutionResult":
        return cls(CellTaskOutcome.GOAL_REJECTED, accepted=False, succeeded=None, detail=detail)

    @classmethod
    def server_unavailable(cls, *, detail: str | None = None) -> "CellTaskExecutionResult":
        return cls(CellTaskOutcome.SERVER_UNAVAILABLE, accepted=False, succeeded=None, detail=detail)

    @classmethod
    def result_timeout(cls, *, detail: str | None = None) -> "CellTaskExecutionResult":
        return cls(CellTaskOutcome.RESULT_TIMEOUT, accepted=True, succeeded=None, detail=detail)

    @classmethod
    def transport_error(cls, *, detail: str | None = None, accepted: bool = False) -> "CellTaskExecutionResult":
        return cls(CellTaskOutcome.ROS_TRANSPORT_ERROR, accepted=accepted, succeeded=None, detail=detail)


def _parse_completed_json(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise CellTaskResultNormalizationError("ExecuteTask Result.completed_json must be a JSON array string.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CellTaskResultNormalizationError("ExecuteTask Result.completed_json must be valid JSON.") from exc
    if not isinstance(parsed, list) or not all(isinstance(slot, str) for slot in parsed):
        raise CellTaskResultNormalizationError(
            "ExecuteTask Result.completed_json must encode an array of string slot identifiers."
        )
    return tuple(parsed)


CellTaskFeedbackCallback = Callable[[CellTaskFeedback], None]
CellTaskAcceptedCallback = Callable[[], None]


class CellActionTransport(Protocol):
    """One logical Action execution, with no SQLAlchemy dependency."""

    def execute(
        self,
        command: CellTaskCommand,
        *,
        goal_accepted_callback: CellTaskAcceptedCallback | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> CellTaskExecutionResult:
        """Send a command and wait for the normalized terminal observation."""
