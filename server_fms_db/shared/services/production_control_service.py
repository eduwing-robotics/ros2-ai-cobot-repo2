"""Durable production pause/resume request and settlement lifecycle.

This service deliberately contains no ROS dependency.  API/Voice use it to
persist a request; the FMS-owned coordinator settles that request only after
physical evidence from the Robot Cell status stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    EventType, ExecutionAttempt, ExecutionAttemptControlState,
    ExecutionAttemptStatus, ExecutorType, JobStatus, ProductionEvent,
    ProductionJob, ProductionJobControlState,
)
from shared.realtime.production_events import get_production_change_callback


class ProductionControlError(RuntimeError):
    pass


class ProductionControlTargetError(ProductionControlError):
    pass


class ProductionControlNoActiveTargetError(ProductionControlTargetError):
    pass


class ProductionControlAmbiguousTargetError(ProductionControlTargetError):
    pass


class ProductionControlConflictError(ProductionControlError):
    pass


class ProductionControlOutcome(StrEnum):
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    ALREADY_PAUSE_REQUESTED = "ALREADY_PAUSE_REQUESTED"
    ALREADY_PAUSED = "ALREADY_PAUSED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    ALREADY_RESUME_REQUESTED = "ALREADY_RESUME_REQUESTED"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    ACTIVE_FORKLIFT_PHYSICAL_PAUSE_NOT_SUPPORTED = "ACTIVE_FORKLIFT_PHYSICAL_PAUSE_NOT_SUPPORTED"


_ACTIVE_ATTEMPT_STATES = (
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
)
_PAUSABLE_JOB_STATES = frozenset({
    JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
    JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY,
})
_NONTERMINAL_JOB_STATES = frozenset({
    JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
    JobStatus.PAUSED, JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY,
})


@dataclass(frozen=True, slots=True)
class ProductionControlResult:
    outcome: ProductionControlOutcome
    job_id: int
    control_req_id: str | None


class ProductionControlService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._callback = get_production_change_callback()

    def request_pause(self, *, job_id: int, immediate: bool = False) -> ProductionControlResult:
        def operation() -> ProductionControlResult:
            job = self._job_for_update(job_id)
            if job.control_state is ProductionJobControlState.PAUSE_REQUESTED:
                return ProductionControlResult(ProductionControlOutcome.ALREADY_PAUSE_REQUESTED, job.job_id, job.control_req_id)
            if job.control_state is ProductionJobControlState.PAUSED:
                return ProductionControlResult(ProductionControlOutcome.ALREADY_PAUSED, job.job_id, job.control_req_id)
            if job.control_state is ProductionJobControlState.RESUME_REQUESTED:
                raise ProductionControlConflictError("Production resume is already requested.")
            if job.status not in _PAUSABLE_JOB_STATES:
                raise ProductionControlConflictError(f"Production job cannot be paused from {job.status.value}.")
            attempts = self._active_attempts_for_update(job.job_id)
            if len(attempts) > 1:
                raise ProductionControlConflictError("Multiple unresolved physical execution attempts prevent a safe pause.")
            if attempts and attempts[0].executor_type is ExecutorType.FORKLIFT:
                return ProductionControlResult(
                    ProductionControlOutcome.ACTIVE_FORKLIFT_PHYSICAL_PAUSE_NOT_SUPPORTED,
                    job.job_id,
                    None,
                )
            request_id = str(uuid4())
            job.control_state = ProductionJobControlState.PAUSE_REQUESTED
            job.control_req_id = request_id
            job.control_requested_at = self._utcnow()
            # This intent belongs to this specific durable PAUSE request. Do
            # not let a prior voice request leak into a later operator pause.
            job.control_immediate_requested = bool(immediate)
            if attempts:
                attempt = attempts[0]
                attempt.control_state = ExecutionAttemptControlState.PAUSE_REQUESTED
                attempt.control_req_id = request_id
                attempt.control_requested_at = job.control_requested_at
                attempt.control_dispatched_at = None
            self._session.flush()
            return ProductionControlResult(ProductionControlOutcome.PAUSE_REQUESTED, job.job_id, request_id)
        return self._transaction(operation, notify=False)

    def resolve_target_job_id(self, *, target_job_id: str | None) -> int:
        """Resolve an explicit target or exactly one non-terminal Job.

        Voice/API is forbidden from choosing the newest active job when more
        than one job is eligible. Mutation and row locking remain in the
        durable pause/resume request methods.
        """
        if target_job_id is not None:
            value = target_job_id.strip()
            if not value.isdecimal():
                raise ProductionControlTargetError("The requested production job id is invalid.")
            job_id = int(value)
            if self._session.get(ProductionJob, job_id) is None:
                raise ProductionControlTargetError("The requested production job was not found.")
            return job_id

        job_ids = list(self._session.scalars(
            select(ProductionJob.job_id)
            .where(ProductionJob.status.in_(_NONTERMINAL_JOB_STATES))
            .order_by(ProductionJob.job_id.asc())
        ))
        if not job_ids:
            raise ProductionControlNoActiveTargetError("No non-terminal production job exists.")
        if len(job_ids) != 1:
            raise ProductionControlAmbiguousTargetError("More than one non-terminal production job exists.")
        return job_ids[0]

    def request_resume(self, *, job_id: int) -> ProductionControlResult:
        def operation() -> ProductionControlResult:
            job = self._job_for_update(job_id)
            if job.status not in _NONTERMINAL_JOB_STATES:
                raise ProductionControlConflictError(f"Production job cannot be resumed from {job.status.value}.")
            if job.control_state is ProductionJobControlState.RESUME_REQUESTED:
                return ProductionControlResult(ProductionControlOutcome.ALREADY_RESUME_REQUESTED, job.job_id, job.control_req_id)
            if job.control_state is ProductionJobControlState.ACTIVE:
                # JobStatus.PAUSED predates durable control state and has no correlated held attempt.
                if job.status is JobStatus.PAUSED:
                    raise ProductionControlConflictError("Legacy paused job status has no durable control lifecycle.")
                return ProductionControlResult(ProductionControlOutcome.ALREADY_ACTIVE, job.job_id, job.control_req_id)
            if job.control_state is not ProductionJobControlState.PAUSED:
                raise ProductionControlConflictError("Production job is not durably paused.")
            attempts = self._held_attempts_for_update(job.job_id)
            if len(attempts) > 1:
                raise ProductionControlConflictError("Multiple held physical execution attempts prevent a safe resume.")
            request_id = str(uuid4())
            job.control_state = ProductionJobControlState.RESUME_REQUESTED
            job.control_req_id = request_id
            job.control_requested_at = self._utcnow()
            job.control_immediate_requested = False
            if attempts:
                attempt = attempts[0]
                attempt.control_state = ExecutionAttemptControlState.RESUME_REQUESTED
                attempt.control_req_id = request_id
                attempt.control_requested_at = job.control_requested_at
                attempt.control_dispatched_at = None
            self._session.flush()
            return ProductionControlResult(ProductionControlOutcome.RESUME_REQUESTED, job.job_id, request_id)
        return self._transaction(operation, notify=False)

    def settle_pause(self, *, job_id: int, attempt_id: int | None) -> ProductionControlResult:
        def operation() -> ProductionControlResult:
            job = self._job_for_update(job_id)
            if job.control_state is ProductionJobControlState.PAUSED:
                return ProductionControlResult(ProductionControlOutcome.ALREADY_PAUSED, job.job_id, job.control_req_id)
            if job.control_state is not ProductionJobControlState.PAUSE_REQUESTED:
                raise ProductionControlConflictError("Production job is not awaiting pause settlement.")
            if attempt_id is not None:
                attempt = self._attempt_for_update(attempt_id)
                if attempt.job_id != job.job_id or attempt.executor_type is not ExecutorType.ROBOT_CELL:
                    raise ProductionControlConflictError("Pause settlement attempt does not belong to this Robot Cell job.")
                if attempt.control_state is not ExecutionAttemptControlState.PAUSE_REQUESTED:
                    raise ProductionControlConflictError("Robot Cell attempt is not awaiting pause settlement.")
                attempt.control_state = ExecutionAttemptControlState.HELD
            job.control_state = ProductionJobControlState.PAUSED
            # HELD is authoritative settlement; the request hint is no longer
            # needed after this point.
            job.control_immediate_requested = False
            self._event(job, EventType.JOB_PAUSED, "Production job paused after Robot Cell HELD confirmation.")
            self._session.flush()
            return ProductionControlResult(ProductionControlOutcome.ALREADY_PAUSED, job.job_id, job.control_req_id)
        return self._transaction(operation, notify=True, reason="job_paused")

    def settle_resume(self, *, job_id: int, attempt_id: int | None) -> ProductionControlResult:
        def operation() -> ProductionControlResult:
            job = self._job_for_update(job_id)
            if job.control_state is ProductionJobControlState.ACTIVE:
                return ProductionControlResult(ProductionControlOutcome.ALREADY_ACTIVE, job.job_id, job.control_req_id)
            if job.control_state is not ProductionJobControlState.RESUME_REQUESTED:
                raise ProductionControlConflictError("Production job is not awaiting resume settlement.")
            if attempt_id is not None:
                attempt = self._attempt_for_update(attempt_id)
                if attempt.job_id != job.job_id or attempt.executor_type is not ExecutorType.ROBOT_CELL:
                    raise ProductionControlConflictError("Resume settlement attempt does not belong to this Robot Cell job.")
                if attempt.control_state is not ExecutionAttemptControlState.RESUME_REQUESTED:
                    raise ProductionControlConflictError("Robot Cell attempt is not awaiting resume settlement.")
                attempt.control_state = ExecutionAttemptControlState.ACTIVE
            job.control_state = ProductionJobControlState.ACTIVE
            job.control_immediate_requested = False
            self._event(job, EventType.JOB_RESUMED, "Production job resumed after Robot Cell EXECUTE confirmation.")
            self._session.flush()
            return ProductionControlResult(ProductionControlOutcome.ALREADY_ACTIVE, job.job_id, job.control_req_id)
        return self._transaction(operation, notify=True, reason="job_resumed")

    def active_attempts(self, *, job_id: int) -> list[ExecutionAttempt]:
        return list(self._session.scalars(select(ExecutionAttempt).where(
            ExecutionAttempt.job_id == job_id,
            ExecutionAttempt.status.in_(_ACTIVE_ATTEMPT_STATES),
        ).order_by(ExecutionAttempt.attempt_id.asc())))

    def _active_attempts_for_update(self, job_id: int) -> list[ExecutionAttempt]:
        return list(self._session.scalars(select(ExecutionAttempt).where(
            ExecutionAttempt.job_id == job_id,
            ExecutionAttempt.status.in_(_ACTIVE_ATTEMPT_STATES),
        ).order_by(ExecutionAttempt.attempt_id.asc()).with_for_update()))

    def _held_attempts_for_update(self, job_id: int) -> list[ExecutionAttempt]:
        return list(self._session.scalars(select(ExecutionAttempt).where(
            ExecutionAttempt.job_id == job_id,
            ExecutionAttempt.control_state.in_((ExecutionAttemptControlState.HELD, ExecutionAttemptControlState.RESUME_REQUESTED)),
        ).order_by(ExecutionAttempt.attempt_id.asc()).with_for_update()))

    def _job_for_update(self, job_id: int) -> ProductionJob:
        job = self._session.scalar(select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update())
        if job is None:
            raise ProductionControlTargetError(f"Production job not found: job_id={job_id}.")
        return job

    def _attempt_for_update(self, attempt_id: int) -> ExecutionAttempt:
        attempt = self._session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.attempt_id == attempt_id).with_for_update())
        if attempt is None:
            raise ProductionControlConflictError("Physical execution attempt disappeared during control settlement.")
        return attempt

    def _event(self, job: ProductionJob, event_type: EventType, message: str) -> None:
        self._session.add(ProductionEvent(job_id=job.job_id, job_step_id=None, event_type=event_type, message=message))

    def _transaction(self, operation, *, notify: bool, reason: str | None = None):
        try:
            result = operation()
            self._session.commit()
            if notify and self._callback is not None:
                job_id = result.job_id if isinstance(result, ProductionControlResult) else None
                # Settlement callers do not need a return payload; their job_id
                # is already known and passed through closure in the coordinator.
                if job_id is not None:
                    self._callback(job_id, reason)
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
