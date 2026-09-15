"""Bridge one FMS execution attempt to existing production state transitions.

The coordinator does not poll for Jobs, perform ROS 2 work, or define pause/cancel
policy. It connects only one already-running Job's current Step to a supplied
transport-neutral executor.
"""

from __future__ import annotations

from dataclasses import dataclass

from fms_server.step_executor import StepExecutionRequest, StepExecutionResult, StepExecutor
from shared.models.factory import JobStep
from shared.services.production_orchestration_service import ProductionOrchestrationService


class NoExecutableStepError(RuntimeError):
    """Raised when a Job has no RUNNING or PENDING Step to execute."""


@dataclass(frozen=True, slots=True)
class StepLifecycleOutcome:
    """One completed coordinator attempt and its DB-backed terminal Step."""

    request: StepExecutionRequest
    execution: StepExecutionResult
    job_step: JobStep


class ProductionStepLifecycleCoordinator:
    """Execute the next DB-selected Step through the existing orchestration rules.

    ``ProductionOrchestrationService`` remains the sole owner of state transitions,
    transactions, events, and row locks. The executor only receives a small request
    and returns success/failure; it never receives a SQLAlchemy Session.
    """

    def __init__(
        self,
        orchestration_service: ProductionOrchestrationService,
        executor: StepExecutor,
    ) -> None:
        self._orchestration_service = orchestration_service
        self._executor = executor

    def execute_next_step(self, job_id: int) -> StepLifecycleOutcome:
        """Start the current Step, execute it, then settle it from its outcome.

        Executor exceptions intentionally propagate. The Step remains RUNNING in
        that case, leaving future FMS recovery policy to a separately agreed design.
        """

        next_step = self._orchestration_service.get_next_step(job_id)
        if next_step is None:
            raise NoExecutableStepError(f"No executable Step is available for job_id={job_id}.")

        running_step = self._orchestration_service.start_step(next_step.job_step_id)
        request = StepExecutionRequest(
            job_id=job_id,
            job_step_id=running_step.job_step_id,
            step_code=running_step.resolved_step_code,
        )
        execution = self._executor.execute(request)

        if execution.succeeded:
            settled_step = self._orchestration_service.complete_step(running_step.job_step_id)
        else:
            settled_step = self._orchestration_service.fail_step(
                running_step.job_step_id,
                reason=execution.failure_reason or "Step executor reported failure.",
            )
        return StepLifecycleOutcome(
            request=request,
            execution=execution,
            job_step=settled_step,
        )
