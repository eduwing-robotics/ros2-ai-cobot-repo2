"""FMS-owned reconciliation of durable Robot Cell pause/resume requests."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from fms_server.cell_status import CellStatusStore
from fms_server.robot_cell_control import RobotCellControlCommand, RobotCellControlPort
from shared.config import get_settings
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptControlState,
    ExecutionAttemptStatus,
    ExecutorType,
    ProductionJob,
    ProductionJobControlState,
)
from shared.services.production_control_service import ProductionControlService

logger = logging.getLogger(__name__)
_ACTIVE = (
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
)


class PauseResumeCoordinator:
    def __init__(
        self,
        session: Session,
        *,
        control_port: RobotCellControlPort,
        cell_status_store: CellStatusStore | None,
    ) -> None:
        self.session = session
        self.port = control_port
        self.status = cell_status_store

    def reconcile_once(self) -> bool:
        jobs = list(self.session.scalars(
            select(ProductionJob)
            .where(ProductionJob.control_state.in_((
                ProductionJobControlState.PAUSE_REQUESTED,
                ProductionJobControlState.RESUME_REQUESTED,
            )))
            .order_by(ProductionJob.job_id)
        ))
        for job in jobs:
            if self._reconcile(job):
                return True
        return False

    def _reconcile(self, job: ProductionJob) -> bool:
        job = self.session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job.job_id).with_for_update()
        )
        if job is None or job.control_state not in (
            ProductionJobControlState.PAUSE_REQUESTED,
            ProductionJobControlState.RESUME_REQUESTED,
        ):
            return False
        attempts = list(self.session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.job_id == job.job_id, ExecutionAttempt.status.in_(_ACTIVE))
            .order_by(ExecutionAttempt.attempt_id)
            .with_for_update()
        ))
        if not attempts:
            service = ProductionControlService(self.session)
            if job.control_state is ProductionJobControlState.PAUSE_REQUESTED:
                service.settle_pause(job_id=job.job_id, attempt_id=None)
            else:
                service.settle_resume(job_id=job.job_id, attempt_id=None)
            return True
        if len(attempts) != 1 or attempts[0].executor_type is not ExecutorType.ROBOT_CELL:
            if len(attempts) == 1:
                self._record_wait_timeout(attempts[0], job, "PAUSE")
            return False
        attempt = attempts[0]
        if job.control_state is ProductionJobControlState.PAUSE_REQUESTED:
            return self._reconcile_pause(job, attempt)
        return self._reconcile_resume(job, attempt)

    def _reconcile_pause(self, job: ProductionJob, attempt: ExecutionAttempt) -> bool:
        # ACK-time cell_state, stop_mode, and eta_ms are deliberately not used
        # here: only a matching HELD telemetry snapshot settles the lifecycle.
        if self._matches_pause_held(attempt, job):
            ProductionControlService(self.session).settle_pause(
                job_id=job.job_id, attempt_id=attempt.attempt_id
            )
            return True
        if attempt.control_dispatched_at is None:
            attempt.control_dispatched_at = job.control_requested_at
            self.session.commit()
            result = self.port.control(
                command=RobotCellControlCommand.PAUSE,
                req_id=job.control_req_id or attempt.req_id,
                immediate=job.control_immediate_requested,
            )
            self._record_ack(result=result, attempt=attempt, action="PAUSE")
            return True
        self._record_wait_timeout(attempt, job, "PAUSE")
        return False

    def _reconcile_resume(self, job: ProductionJob, attempt: ExecutionAttempt) -> bool:
        if self._resume_unverified(attempt):
            attempt.detail = "CELL_CONTROL_RESUME_UNVERIFIED: physical held-part verification required."
            self.session.commit()
            logger.warning("Robot Cell resume remains unverified for attempt_id=%s", attempt.attempt_id)
            return False
        if self._matches_execute(attempt):
            ProductionControlService(self.session).settle_resume(
                job_id=job.job_id, attempt_id=attempt.attempt_id
            )
            return True
        if self._held_not_resumable(attempt):
            attempt.detail = "CELL_CONTROL_RESUME_NOT_RESUMABLE_RESET_REQUIRED"
            self.session.commit()
            logger.warning("Robot Cell HELD task is not resumable: attempt_id=%s", attempt.attempt_id)
            return False
        if attempt.control_dispatched_at is None:
            attempt.control_dispatched_at = job.control_requested_at
            self.session.commit()
            result = self.port.control(
                command=RobotCellControlCommand.RESUME,
                req_id=job.control_req_id or attempt.req_id,
            )
            self._record_ack(result=result, attempt=attempt, action="RESUME")
            return True
        self._record_wait_timeout(attempt, job, "RESUME")
        return False

    def _record_ack(self, *, result, attempt: ExecutionAttempt, action: str) -> None:
        if not result.accepted:
            attempt.detail = f"CELL_CONTROL_{action}_REJECTED: {result.detail or 'rejected'}"
            self.session.commit()
            return
        logger.info(
            "Robot Cell %s ACK accepted: attempt_id=%s cell_state=%s stop_mode=%s eta_ms=%s",
            action,
            attempt.attempt_id,
            result.cell_state,
            result.stop_mode,
            result.eta_ms,
        )

    def _record_wait_timeout(self, attempt: ExecutionAttempt, job: ProductionJob, action: str) -> None:
        if job.control_requested_at is None:
            return
        age = (datetime.now(timezone.utc) - job.control_requested_at).total_seconds()
        if age >= get_settings().cell_action_result_timeout_seconds:
            attempt.detail = f"CELL_CONTROL_{action}_STATUS_TIMEOUT"
            self.session.commit()

    def _snapshot(self):
        return self.status.latest() if self.status is not None else None

    @staticmethod
    def _task_matches(snapshot, attempt: ExecutionAttempt) -> bool:
        task = snapshot.active_task
        return bool(
            task
            and str(task.get("req_id")) == attempt.req_id
            and str(task.get("job_id")) == str(attempt.job_id)
            and str(task.get("step_id")) == str(attempt.job_step_id)
        )

    def _matches_pause_held(self, attempt: ExecutionAttempt, job: ProductionJob) -> bool:
        snapshot = self._snapshot()
        if snapshot is None or snapshot.cell_state != "HELD" or not self._task_matches(snapshot, attempt):
            return False
        # v0.2 had only active_task correlation. v0.3 adds a second, required
        # pause request correlation when hold metadata is present.
        hold = snapshot.hold
        if hold is None:
            return True
        return (
            str(hold.get("task_req_id")) == attempt.req_id
            and str(hold.get("pause_req_id")) == str(job.control_req_id)
        )

    def _matches_execute(self, attempt: ExecutionAttempt) -> bool:
        snapshot = self._snapshot()
        return bool(
            snapshot is not None
            and snapshot.cell_state == "EXECUTE"
            and self._task_matches(snapshot, attempt)
            and not self._resume_unverified(attempt, snapshot=snapshot)
        )

    def _held_not_resumable(self, attempt: ExecutionAttempt) -> bool:
        snapshot = self._snapshot()
        return bool(
            snapshot is not None
            and snapshot.cell_state == "HELD"
            and self._task_matches(snapshot, attempt)
            and snapshot.hold is not None
            and snapshot.hold.get("resumable") is False
        )

    def _resume_unverified(self, attempt: ExecutionAttempt, *, snapshot=None) -> bool:
        snapshot = snapshot or self._snapshot()
        # The current status model's bounded diagnostics field is error. Do not
        # promote this Robot Cell warning to a successful EXECUTE confirmation.
        return bool(
            snapshot is not None
            and self._task_matches(snapshot, attempt)
            and snapshot.error == "RESUME_UNVERIFIED"
        )
