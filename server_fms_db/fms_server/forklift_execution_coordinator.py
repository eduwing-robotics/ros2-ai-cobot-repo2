import json
import logging
from typing import Callable

from sqlalchemy.orm import Session

from fms_server.forklift_action_adapter import (
    ForkliftActionAdapter,
    ForkliftExecutionFeedback,
    ForkliftExecutionResult,
    ForkliftFeedbackCallback,
)
from shared.config import Settings, get_settings
from shared.equipment_registry import equipment
from fms_server.forklift_runtime_state import ForkliftRuntimeEvent, ForkliftRuntimeState
from shared.models.factory import ExecutorType, ExecutionAttemptStatus
from shared.services.execution_attempt_service import ExecutionAttemptService, ExecutionAttemptConflictError

logger = logging.getLogger(__name__)


class ForkliftExecutionCoordinator:
    """Coordinate logical delivery state with Forklift Action Adapter and Execution Attempts."""

    def __init__(
        self,
        session: Session,
        *,
        adapter: ForkliftActionAdapter,
        execution_attempt_service: ExecutionAttemptService,
        feedback_sink: ForkliftFeedbackCallback | None = None,
        runtime_state_sink: Callable[[ForkliftRuntimeState], None] | None = None,
        runtime_state_cleanup_sink: Callable[[str, str], None] | None = None,
        runtime_event_sink: Callable[[ForkliftRuntimeEvent], None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._adapter = adapter
        self._execution_attempt_service = execution_attempt_service
        self._feedback_sink = feedback_sink
        self._runtime_state_sink = runtime_state_sink
        self._runtime_state_cleanup_sink = runtime_state_cleanup_sink
        self._runtime_event_sink = runtime_event_sink
        self._last_feedback_by_req_id: dict[str, ForkliftRuntimeState] = {}
        resolved_settings = settings or get_settings()
        self._transport_robot_id = equipment(resolved_settings.turtlebot_robot_id, resolved_settings).robot_id

    def execute_transport(
        self,
        job_id: int,
        delivery_id: int,
        pickup_code: str,
        dropoff_code: str,
        req_id: str | None = None,
        attempt_preclaimed: bool = False,
        command_type: str = "EXECUTE_TRANSPORT",
    ) -> ForkliftExecutionResult:
        dispatch_req_id = req_id

        payload_dict = {
            "job_id": job_id,
            "delivery_id": delivery_id,
            "pickup_code": pickup_code,
            "dropoff_code": dropoff_code,
        }
        # req_id is durable metadata for the explicit Phase B cleanup Attempt,
        # not an adapter/ROS action argument.
        if command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN" and dispatch_req_id:
            payload_dict["req_id"] = dispatch_req_id

        try:
            if attempt_preclaimed:
                if not dispatch_req_id:
                    raise ExecutionAttemptConflictError("A preclaimed transport requires req_id.")
                attempt = self._execution_attempt_service.get_by_req_id(dispatch_req_id)
                if (
                    attempt.executor_type is not ExecutorType.FORKLIFT
                    or attempt.command_type != command_type
                    or attempt.job_id != job_id
                    or attempt.job_delivery_id != delivery_id
                    or attempt.request_payload_json != json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
                ):
                    raise ExecutionAttemptConflictError("Preclaimed transport attempt does not match the requested delivery payload.")
            else:
                attempt = self._execution_attempt_service.create_attempt(
                    executor_type=ExecutorType.FORKLIFT,
                    command_type=command_type,
                    request_payload=payload_dict,
                    job_id=job_id,
                    job_delivery_id=delivery_id,
                    req_id=dispatch_req_id,
                )
                dispatch_req_id = attempt.req_id
        except ExecutionAttemptConflictError as exc:
            logger.warning("Forklift req_id conflict: %s", exc)
            return ForkliftExecutionResult(
                status="FAILED",  # type: ignore
                error_code="CONFLICT",
                detail=str(exc)
            )

        # This is committed by ExecutionAttemptService before adapter/network I/O.
        # The current action adapter exposes no accepted callback, so this code
        # deliberately does not fabricate durable ACCEPTED evidence.
        self._execution_attempt_service.mark_dispatching(dispatch_req_id)

        result = self._adapter.dispatch_execute_transport(
            req_id=dispatch_req_id,
            job_id=job_id,
            delivery_id=delivery_id,
            pickup_code=pickup_code,
            dropoff_code=dropoff_code,
            feedback_callback=self._bind_feedback(
                req_id=dispatch_req_id,
                job_id=job_id,
                delivery_id=delivery_id,
                task_type=command_type,
            ),
        )

        result_payload = {
            "status": result.status.value,
            "error_code": result.error_code,
        }

        # Apply terminal state based on status
        if result.status.value == "SUCCEEDED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.SUCCEEDED,
                result_payload=result_payload,
            )
        elif result.status.value == "FAILED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.FAILED,
                result_payload=result_payload,
                error_code=result.error_code,
                detail=result.detail,
            )
        elif result.status.value == "CANCELED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.CANCELED,
                result_payload=result_payload,
            )
        else:
            self._execution_attempt_service.mark_unknown(dispatch_req_id, detail=f"Unknown transport status {result.status}")

        self._emit_terminal_runtime_event(dispatch_req_id, result)
        self._clear_runtime_state(dispatch_req_id)

        return result

    def return_home(
        self,
        req_id: str | None = None,
        *,
        job_id: int | None = None,
        attempt_preclaimed: bool = False,
    ) -> ForkliftExecutionResult:
        """Dispatch the separate ReturnHome Action with durable Attempt evidence.

        ``job_id`` is FMS-only Attempt correlation; the TurtleBot Goal remains
        the confirmed single-field ``req_id`` contract.
        """
        dispatch_req_id = req_id
        payload_dict = {"job_id": job_id} if job_id is not None else {}

        try:
            if attempt_preclaimed:
                if not dispatch_req_id:
                    raise ExecutionAttemptConflictError("A preclaimed ReturnHome requires req_id.")
                attempt = self._execution_attempt_service.get_by_req_id(dispatch_req_id)
                if (
                    attempt.executor_type is not ExecutorType.FORKLIFT
                    or attempt.command_type != "RETURN_HOME"
                    or attempt.job_id != job_id
                    or attempt.job_delivery_id is not None
                    or attempt.request_payload_json != json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
                ):
                    raise ExecutionAttemptConflictError(
                        "Preclaimed ReturnHome attempt does not match the requested job payload."
                    )
            else:
                attempt = self._execution_attempt_service.create_attempt(
                    executor_type=ExecutorType.FORKLIFT,
                    command_type="RETURN_HOME",
                    request_payload=payload_dict,
                    job_id=job_id,
                    req_id=dispatch_req_id,
                )
                dispatch_req_id = attempt.req_id
        except ExecutionAttemptConflictError as exc:
            logger.warning("Forklift req_id conflict: %s", exc)
            return ForkliftExecutionResult(
                status="FAILED",  # type: ignore
                error_code="CONFLICT",
                detail=str(exc)
            )

        self._execution_attempt_service.mark_dispatching(dispatch_req_id)

        result = self._adapter.dispatch_return_home(
            req_id=dispatch_req_id,
            feedback_callback=self._bind_feedback(
                req_id=dispatch_req_id,
                job_id=None,
                delivery_id=None,
                task_type="RETURN_HOME",
            ),
        )

        result_payload = {
            "status": result.status.value,
            "error_code": result.error_code,
        }

        if result.status.value == "SUCCEEDED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.SUCCEEDED,
                result_payload=result_payload,
            )
        elif result.status.value == "FAILED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.FAILED,
                result_payload=result_payload,
                error_code=result.error_code,
                detail=result.detail,
            )
        elif result.status.value == "CANCELED":
            self._execution_attempt_service.apply_result(
                dispatch_req_id,
                ExecutionAttemptStatus.CANCELED,
                result_payload=result_payload,
            )
        else:
            self._execution_attempt_service.mark_unknown(dispatch_req_id, detail=f"Unknown transport status {result.status}")

        self._emit_terminal_runtime_event(dispatch_req_id, result)
        self._clear_runtime_state(dispatch_req_id)

        return result

    def _bind_feedback(
        self, *, req_id: str, job_id: int | None, delivery_id: int | None, task_type: str
    ) -> ForkliftFeedbackCallback:
        def callback(feedback: ForkliftExecutionFeedback) -> None:
            self._on_feedback(feedback)
            state = ForkliftRuntimeState(
                req_id=req_id, job_id=job_id, delivery_id=delivery_id,
                robot_id=self._transport_robot_id, task_type=task_type,
                phase=feedback.phase, progress=feedback.progress, detail=feedback.detail,
            )
            self._last_feedback_by_req_id[req_id] = state
            if self._runtime_state_sink is not None:
                try:
                    self._runtime_state_sink(state)
                except Exception as exc:
                    logger.warning("Could not publish Forklift runtime feedback: %s", exc)
        return callback

    def _emit_terminal_runtime_event(self, req_id: str, result: ForkliftExecutionResult) -> None:
        if result.status.value not in {"SUCCEEDED", "FAILED", "CANCELED"}:
            logger.warning("Skipping uncontracted Forklift terminal result req_id=%s status=%s", req_id, result.status.value)
            self._last_feedback_by_req_id.pop(req_id, None)
            return
        state = self._last_feedback_by_req_id.pop(req_id, None)
        if state is None:
            logger.info("Skipping terminal Forklift transport event without actual feedback req_id=%s", req_id)
            return
        if self._runtime_event_sink is None:
            return
        try:
            self._runtime_event_sink(
                ForkliftRuntimeEvent(
                    state=state, result=result.status.value,
                    error_code=result.error_code, detail=result.detail,
                )
            )
        except Exception as exc:
            logger.warning("Could not publish Forklift terminal runtime event: %s", exc)

    def _clear_runtime_state(self, req_id: str) -> None:
        if self._runtime_state_cleanup_sink is None:
            return
        try:
            self._runtime_state_cleanup_sink(self._transport_robot_id, req_id)
        except Exception as exc:
            logger.warning("Could not clear Forklift runtime feedback: %s", exc)

    def _on_feedback(self, feedback: ForkliftExecutionFeedback) -> None:
        if self._feedback_sink is not None:
            self._feedback_sink(feedback)
