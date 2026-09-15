"""Minimal FMS boundary for one externally executed production Step.

No ROS 2 Action type, Robot Cell payload, feedback format, or hardware command is
assumed here. Future adapters may implement ``StepExecutor`` only after their
separate transport contracts are agreed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StepExecutionRequest:
    """DB-derived minimum context for an external Step execution attempt."""

    job_id: int
    job_step_id: int
    step_code: str


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    """Terminal outcome reported by an executor boundary."""

    succeeded: bool
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.succeeded and self.failure_reason is not None:
            raise ValueError("A successful Step execution must not include a failure reason.")
        if not self.succeeded and not (self.failure_reason and self.failure_reason.strip()):
            raise ValueError("A failed Step execution requires a non-empty failure reason.")

    @classmethod
    def success(cls) -> "StepExecutionResult":
        return cls(succeeded=True)

    @classmethod
    def failed(cls, reason: str) -> "StepExecutionResult":
        return cls(succeeded=False, failure_reason=reason.strip())


class StepExecutor(Protocol):
    """Execute one Step and return a terminal outcome.

    This synchronous protocol matches the current synchronous SQLAlchemy production
    service. It is intentionally transport-neutral; it is not a ROS 2 interface.
    """

    def execute(self, request: StepExecutionRequest) -> StepExecutionResult:
        """Run one externally owned Step without mutating production DB state."""
