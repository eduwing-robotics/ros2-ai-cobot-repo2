"""Compose readiness, Cell Action dispatch, and existing production transitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fms_server.cell_action_transport import CellTaskExecutionResult, CellTaskFeedbackCallback
from fms_server.cell_status import CellStatusStore
from fms_server.robot_cell_action_adapter import (
    CellTaskResultContractError,
    RobotCellActionAdapter,
    RobotCellActionAdapterError,
)
from shared.models.factory import JobStatus, JobStep, ProductionJob, ProductionJobControlState, StepStatus
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService

logger = logging.getLogger(__name__)


class CoordinatorOutcome(StrEnum):
    """FMS-level dispatch outcome, distinct from Robot Cell error codes."""

    COMPLETED = "COMPLETED"
    CELL_FAILED = "CELL_FAILED"
    CELL_CANCELED = "CELL_CANCELED"
    NOT_DISPATCHED_NOT_READY = "NOT_DISPATCHED_NOT_READY"
    GOAL_REJECTED = "GOAL_REJECTED"
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    UNCERTAIN_RESULT_TIMEOUT = "UNCERTAIN_RESULT_TIMEOUT"
    UNCERTAIN_TRANSPORT_ERROR = "UNCERTAIN_TRANSPORT_ERROR"
    CONTRACT_BLOCKED = "CONTRACT_BLOCKED"
    CELL_UNAVAILABLE = "CELL_UNAVAILABLE"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_CURRENT = "NOT_CURRENT"


@dataclass(frozen=True, slots=True)
class ExecutionCoordinatorResult:
    """One FMS execution attempt without a new persistence/dispatch ledger."""

    outcome: CoordinatorOutcome
    job_id: int
    job_step_id: int
    req_id: str
    cell_result: CellTaskExecutionResult | None = None
    started: bool = False
    requires_reconciliation: bool = False
    detail: str | None = None


class FmsExecutionCoordinator:
    """Run one already-selected current Step through the Robot Cell boundary.

    This coordinator owns no state machine. It asks the existing orchestration
    service for the current Step, asks the readiness boundary for prerequisites,
    and delegates every DB state transition back to the orchestration service.
    Transport callbacks never receive the SQLAlchemy Session.
    """

    def __init__(
        self,
        session: Session,
        *,
        orchestration_service: ProductionOrchestrationService,
        step_readiness_service: StepReadinessService,
        robot_cell_adapter: RobotCellActionAdapter,
        execution_attempt_service: 'ExecutionAttemptService',
        cell_status_store: CellStatusStore | None = None,
        post_commit_callback: callable | None = None,
    ) -> None:
        self._session = session
        self._orchestration = orchestration_service
        self._step_readiness = step_readiness_service
        self._adapter = robot_cell_adapter
        self._execution_attempt_service = execution_attempt_service
        self._cell_status_store = cell_status_store
        if post_commit_callback is not None:
            self._orchestration.set_post_commit_callback(post_commit_callback)

    def execute_step(
        self,
        *,
        job_id: int,
        job_step_id: int,
        parts_json: str,
        req_id: str | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> ExecutionCoordinatorResult:
        """Dispatch one PENDING current Step and settle only authoritative results."""
        if req_id is None:
            # Generate temporary ID for error responses if req_id was not provided
            error_req_id = ""
        else:
            error_req_id = req_id

        next_step = self._orchestration.get_next_step(job_id)
        if next_step is None:
            requested = self._session.get(JobStep, job_step_id)
            if requested is not None and requested.job_id == job_id and requested.status in {
                StepStatus.COMPLETED,
                StepStatus.FAILED,
                StepStatus.CANCELED,
            }:
                return self._result(CoordinatorOutcome.ALREADY_TERMINAL, job_id, job_step_id, error_req_id)
            return self._result(CoordinatorOutcome.NOT_CURRENT, job_id, job_step_id, error_req_id)
        if next_step.job_step_id != job_step_id:
            requested = self._session.get(JobStep, job_step_id)
            if requested is not None and requested.job_id == job_id and requested.status in {
                StepStatus.COMPLETED,
                StepStatus.FAILED,
                StepStatus.CANCELED,
            }:
                return self._result(CoordinatorOutcome.ALREADY_TERMINAL, job_id, job_step_id, error_req_id)
            return self._result(CoordinatorOutcome.NOT_CURRENT, job_id, job_step_id, error_req_id)
        if next_step.status is StepStatus.RUNNING:
            return self._result(
                CoordinatorOutcome.ALREADY_RUNNING,
                job_id,
                job_step_id,
                error_req_id,
                requires_reconciliation=True,
            )
        if next_step.status in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.CANCELED}:
            return self._result(CoordinatorOutcome.ALREADY_TERMINAL, job_id, job_step_id, error_req_id)
        if next_step.status is not StepStatus.PENDING:
            return self._result(CoordinatorOutcome.NOT_CURRENT, job_id, job_step_id, error_req_id)

        job, step = self._load_dispatch_context(job_id=job_id, job_step_id=job_step_id)
        if not self._can_dispatch_for_job_status(job, step):
            return self._result(CoordinatorOutcome.NOT_CURRENT, job_id, job_step_id, error_req_id)
        readiness = self._step_readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)
        if not readiness.ready:
            return self._result(CoordinatorOutcome.NOT_DISPATCHED_NOT_READY, job_id, job_step_id, error_req_id)

        # This is before ExecutionAttempt creation and Action send. A heartbeat loss
        # gates only new work; it never changes an already-dispatched Action.
        if self._cell_status_store is not None:
            availability = self._cell_status_store.dispatch_availability()
            if not availability.dispatch_allowed:
                logger.warning("Blocking new Cell dispatch job_id=%s step_id=%s reason=%s", job_id, job_step_id, availability.reason)
                return self._result(CoordinatorOutcome.CELL_UNAVAILABLE, job_id, job_step_id, error_req_id, detail=availability.reason)

        # Reject an empty or malformed physical Goal before an ExecutionAttempt
        # is created. A CREATED row for a command that can never be sent would
        # otherwise look like an ambiguous dispatch after restart.
        try:
            self._adapter.requested_slots_from_parts_json(parts_json)
        except RobotCellActionAdapterError as exc:
            logger.warning(
                "Cell dispatch contract blocked before attempt creation job_id=%s step_id=%s detail=%s",
                job_id,
                job_step_id,
                exc,
            )
            return self._result(
                CoordinatorOutcome.CONTRACT_BLOCKED,
                job_id,
                job_step_id,
                error_req_id,
                detail=str(exc),
            )

        # Before dispatching, build the task command to get the exact payload for idempotency.
        # But we need req_id for that. So we generate req_id inside build_command?
        # Actually, ExecutionAttemptService can create req_id, but the command needs it.
        # We can ask ExecutionAttemptService to create the attempt first.
        # What is the request_payload? It's the dictionary representation of CellTaskCommand.
        # Let's use a dummy req_id to build the command, then we serialize it?
        # Wait! The adapter expects a string req_id. Let's just create it here if None.
        dispatch_req_id = req_id

        try:
            from fms_server.robot_cell_action_adapter import RobotCellTaskTypeMapper
            mapped_task_type = RobotCellTaskTypeMapper.map_operation_code(step.operation_code)

            payload_dict = {
                "job_id": job.job_id,
                "step_id": step.job_step_id,
                "product": job.product.product_code.strip() if job.product is not None else "",
                "parts_json": parts_json,
            }

            # M4: Record the attempt
            from shared.models.factory import ExecutorType
            from shared.services.execution_attempt_service import ExecutionAttemptConflictError
            try:
                attempt = self._execution_attempt_service.create_attempt(
                    executor_type=ExecutorType.ROBOT_CELL,
                    command_type=mapped_task_type,
                    request_payload=payload_dict,
                    job_id=job.job_id,
                    job_step_id=step.job_step_id,
                    req_id=dispatch_req_id,
                )
                dispatch_req_id = attempt.req_id
            except ExecutionAttemptConflictError as exc:
                logger.warning("req_id conflict: %s", exc)
                return self._result(CoordinatorOutcome.CONTRACT_BLOCKED, job_id, job_step_id, dispatch_req_id, detail=str(exc))

            command = self._adapter.build_command(job=job, step=step, req_id=dispatch_req_id, parts_json=parts_json)

            started = False

            def on_goal_accepted() -> None:
                nonlocal started
                self._execution_attempt_service.mark_accepted(dispatch_req_id)
                # Goal acceptance is the first durable confirmation that the
                # Robot Cell has entered execution. The orchestration method
                # issues reserved material and persists RUNNING atomically.
                self._orchestration.start_step_after_execution_accepted(step.job_step_id)
                started = True

            self._execution_attempt_service.mark_dispatching(dispatch_req_id)

            cell_result = self._adapter.execute(
                command,
                goal_accepted_callback=on_goal_accepted,
                feedback_callback=feedback_callback,
            )
        except RobotCellActionAdapterError as exc:
            logger.warning(
                "Cell dispatch contract blocked req_id=%s job_id=%s step_id=%s detail=%s",
                req_id,
                job_id,
                job_step_id,
                exc,
            )
            return self._result(
                CoordinatorOutcome.CONTRACT_BLOCKED,
                job_id,
                job_step_id,
                req_id,
                detail=str(exc),
            )

        logger.info(
            "Cell dispatch outcome req_id=%s job_id=%s step_id=%s outcome=%s cell_error_code=%s",
            dispatch_req_id,
            job_id,
            job_step_id,
            cell_result.outcome,
            cell_result.error_code,
        )

        result_payload = {
            "outcome": cell_result.outcome.value,
            "error_code": cell_result.error_code,
        }

        from shared.models.factory import ExecutionAttemptStatus

        if cell_result.succeeded is True:
            if not started:
                self._execution_attempt_service.mark_unknown(dispatch_req_id, detail="Transport reported success without delivering goal acceptance.")
                return self._result(
                    CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR,
                    job_id,
                    job_step_id,
                    dispatch_req_id,
                    cell_result=cell_result,
                    requires_reconciliation=True,
                    detail="Transport reported success without delivering goal acceptance.",
                )
            try:
                self._adapter.validate_successful_result(command=command, result=cell_result)
            except CellTaskResultContractError as exc:
                # The Cell reports SUCCESS, but its result cannot certify that
                # every requested item completed. Preserve the physical
                # ambiguity for existing recovery/reconciliation rather than
                # fabricating a Step failure or completion.
                self._execution_attempt_service.mark_unknown(dispatch_req_id, detail=str(exc))
                return self._result(
                    CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR,
                    job_id,
                    job_step_id,
                    dispatch_req_id,
                    cell_result=cell_result,
                    started=True,
                    requires_reconciliation=True,
                    detail=str(exc),
                )
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.SUCCEEDED,
                result_payload=result_payload,
            )
            self._orchestration.complete_step(step.job_step_id)
            return self._result(CoordinatorOutcome.COMPLETED, job_id, job_step_id, dispatch_req_id, cell_result=cell_result, started=True)

        if cell_result.succeeded is False:
            if not started:
                self._execution_attempt_service.mark_unknown(dispatch_req_id, detail="Transport reported a Cell failure without delivering goal acceptance.")
                return self._result(
                    CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR,
                    job_id,
                    job_step_id,
                    dispatch_req_id,
                    cell_result=cell_result,
                    requires_reconciliation=True,
                    detail="Transport reported a Cell failure without delivering goal acceptance.",
                )
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.FAILED,
                result_payload=result_payload,
                error_code=cell_result.error_code,
                detail=self._failure_reason(cell_result),
            )
            self._orchestration.fail_step(
                step.job_step_id,
                reason=self._failure_reason(cell_result),
                error_code=cell_result.error_code,
            )
            return self._result(CoordinatorOutcome.CELL_FAILED, job_id, job_step_id, dispatch_req_id, cell_result=cell_result, started=True)

        if cell_result.outcome.value == "CELL_CANCELED":
            # Job/Step enums include CANCELED, but no authoritative public
            # cancellation transition currently owns Cell-originated cancellation.
            # Preserve a started Step as RUNNING rather than misclassifying it FAILED.
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.CANCELED,
                result_payload=result_payload,
            )
            return self._result(
                CoordinatorOutcome.CELL_CANCELED,
                job_id,
                job_step_id,
                dispatch_req_id,
                cell_result=cell_result,
                started=started,
                requires_reconciliation=started,
            )

        if cell_result.outcome.value == "GOAL_REJECTED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.FAILED,
                result_payload=result_payload,
                detail="GOAL_REJECTED",
            )
            return self._result(CoordinatorOutcome.GOAL_REJECTED, job_id, job_step_id, dispatch_req_id, cell_result=cell_result)

        if cell_result.outcome.value == "SERVER_UNAVAILABLE":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.FAILED,
                result_payload=result_payload,
                detail="SERVER_UNAVAILABLE",
            )
            return self._result(CoordinatorOutcome.SERVER_UNAVAILABLE, job_id, job_step_id, dispatch_req_id, cell_result=cell_result)

        if cell_result.outcome.value == "RESULT_TIMEOUT":
            self._execution_attempt_service.mark_unknown(dispatch_req_id, detail="RESULT_TIMEOUT")
            return self._result(
                CoordinatorOutcome.UNCERTAIN_RESULT_TIMEOUT,
                job_id,
                job_step_id,
                dispatch_req_id,
                cell_result=cell_result,
                started=started,
                requires_reconciliation=started,
            )

        self._execution_attempt_service.mark_unknown(dispatch_req_id, detail=f"Transport outcome: {cell_result.outcome.value}")
        return self._result(
            CoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR,
            job_id,
            job_step_id,
            dispatch_req_id,
            cell_result=cell_result,
            started=started,
            requires_reconciliation=started,
        )

    def _load_dispatch_context(self, *, job_id: int, job_step_id: int) -> tuple[ProductionJob, JobStep]:
        job = self._session.scalar(
            select(ProductionJob)
            .options(joinedload(ProductionJob.product))
            .where(ProductionJob.job_id == job_id)
        )
        step = self._session.scalar(select(JobStep).where(JobStep.job_step_id == job_step_id))
        if job is None or step is None or step.job_id != job.job_id:
            raise RuntimeError("Current JobStep disappeared or no longer belongs to the requested ProductionJob.")
        return job, step

    @staticmethod
    def _can_dispatch_for_job_status(job: ProductionJob, step: JobStep) -> bool:
        if job.control_state is not ProductionJobControlState.ACTIVE:
            return False
        if step.operation_code == "INSTALL_ROOF":
            return job.status is JobStatus.ROOF_READY
        return job.status is JobStatus.RUNNING

    @staticmethod
    def _failure_reason(result: CellTaskExecutionResult) -> str:
        message = result.detail.strip() if result.detail else "Robot Cell reported an execution failure."
        # error_code travels separately to ProductionEvent.error_code.  Do not make
        # failure_reason parsing a secondary source of truth for Cell errors.
        return message

    @staticmethod
    def _result(
        outcome: CoordinatorOutcome,
        job_id: int,
        job_step_id: int,
        req_id: str,
        *,
        cell_result: CellTaskExecutionResult | None = None,
        started: bool = False,
        requires_reconciliation: bool = False,
        detail: str | None = None,
    ) -> ExecutionCoordinatorResult:
        return ExecutionCoordinatorResult(
            outcome=outcome,
            job_id=job_id,
            job_step_id=job_step_id,
            req_id=req_id,
            cell_result=cell_result,
            started=started,
            requires_reconciliation=requires_reconciliation,
            detail=detail,
        )
