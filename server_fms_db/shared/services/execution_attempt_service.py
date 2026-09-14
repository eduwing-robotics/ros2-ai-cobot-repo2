import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from sqlalchemy import event, select, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
)
from shared.realtime.error_events import get_execution_attempt_error_callback

logger = logging.getLogger(__name__)

_Result = TypeVar("_Result")

class BaseFactoryError(Exception):
    pass

class ResourceNotFoundError(BaseFactoryError):
    pass

class ExecutionAttemptNotFoundError(ResourceNotFoundError):
    pass

class ExecutionAttemptConflictError(BaseFactoryError):
    pass

class ExecutionAttemptStateTransitionError(BaseFactoryError):
    pass


class ExecutionAttemptService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._install_error_notification_hooks()

    def _install_error_notification_hooks(self) -> None:
        self._install_error_notification_hooks_for(self._session)

    @staticmethod
    def _install_error_notification_hooks_for(target_session: Session) -> None:
        """Notify only after the owning transaction commits successfully."""
        if target_session.info.get("execution_attempt_error_hooks_installed"):
            return
        target_session.info["execution_attempt_error_hooks_installed"] = True

        def after_commit(session: Session) -> None:
            attempt_ids = session.info.pop("execution_attempt_error_attempt_ids", set())
            callback = get_execution_attempt_error_callback()
            if callback is None:
                return
            for attempt_id in attempt_ids:
                try:
                    callback(attempt_id)
                except Exception as exc:
                    # A realtime notification is never allowed to alter a committed result.
                    logger.warning("Could not enqueue committed execution-attempt error %s: %s", attempt_id, exc)

        def after_rollback(session: Session) -> None:
            session.info.pop("execution_attempt_error_attempt_ids", None)

        event.listen(target_session, "after_commit", after_commit)
        event.listen(target_session, "after_rollback", after_rollback)

    def _queue_error_notification(self, attempt: ExecutionAttempt) -> None:
        self._session.info.setdefault("execution_attempt_error_attempt_ids", set()).add(attempt.attempt_id)

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _run_in_new_transaction(self, operation: Callable[[Session], _Result]) -> _Result:
        engine = self._session.get_bind()
        with Session(engine, expire_on_commit=False) as new_session:
            self._install_error_notification_hooks_for(new_session)
            result = operation(new_session)
            new_session.commit()
            # The owner session may already have loaded the same Attempt.  Do
            # not leave it presenting stale CREATED/active state after the
            # independent durability transaction commits.
            self._session.expire_all()
            return result

    def create_attempt(
        self,
        *,
        executor_type: ExecutorType,
        command_type: str,
        request_payload: dict[str, Any],
        job_id: int | None = None,
        job_step_id: int | None = None,
        job_delivery_id: int | None = None,
        req_id: str | None = None,
    ) -> ExecutionAttempt:
        """Create a new physical execution attempt with a new req_id, or return existing on idempotent req_id."""

        canonical_payload = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))

        is_auto_generated = (req_id is None)

        def _operation(session: Session) -> ExecutionAttempt:
            nonlocal req_id

            # 1. UUID generation / Collision Retry Loop
            for uuid_retry in range(5):
                if is_auto_generated:
                    req_id = str(uuid.uuid4())

                # Check idempotency for caller-supplied
                if not is_auto_generated:
                    existing = session.scalar(
                        select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id)
                    )
                    if existing is not None:
                        if existing.request_payload_json == canonical_payload:
                            return existing
                        raise ExecutionAttemptConflictError(
                            f"req_id {req_id} was already used with a different payload."
                        )

                # 2. Attempt No Allocation Loop
                for retry in range(5):
                    stmt = select(func.max(ExecutionAttempt.attempt_no))
                    if job_step_id is not None:
                        stmt = stmt.where(
                            ExecutionAttempt.job_step_id == job_step_id,
                            ExecutionAttempt.command_type == command_type
                        )
                    elif job_delivery_id is not None:
                        stmt = stmt.where(
                            ExecutionAttempt.job_delivery_id == job_delivery_id,
                            ExecutionAttempt.command_type == command_type
                        )

                    max_attempt_no = 0
                    if job_step_id is not None or job_delivery_id is not None:
                        max_attempt_no = session.scalar(stmt) or 0

                    attempt = ExecutionAttempt(
                        req_id=req_id,
                        executor_type=executor_type,
                        command_type=command_type,
                        job_id=job_id,
                        job_step_id=job_step_id,
                        job_delivery_id=job_delivery_id,
                        attempt_no=max_attempt_no + 1,
                        status=ExecutionAttemptStatus.CREATED,
                        request_payload_json=canonical_payload,
                        created_at=self._utcnow()
                    )

                    try:
                        with session.begin_nested():
                            session.add(attempt)
                            session.flush()
                        return attempt
                    except IntegrityError as exc:
                        if "uq_execution_attempts_job_step_command_attempt" in str(exc) or "uq_execution_attempts_job_delivery_command_attempt" in str(exc):
                            if retry == 4:
                                raise ExecutionAttemptConflictError("Max retries exceeded for attempt_no allocation") from exc
                            continue
                        if "execution_attempts_req_id_key" in str(exc):
                            if is_auto_generated:
                                # Break out of attempt_no loop to retry UUID generation
                                break
                            else:
                                raise ExecutionAttemptConflictError(f"req_id {req_id} was already used.") from exc
                        raise
                else:
                    raise ExecutionAttemptConflictError("Unexpected loop exit during attempt_no allocation")
            raise ExecutionAttemptConflictError("Max retries exceeded for UUID collision")

        return self._run_in_new_transaction(_operation)

    def get_by_req_id(self, req_id: str) -> ExecutionAttempt:
        attempt = self._session.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id)
        )
        if not attempt:
            raise ExecutionAttemptNotFoundError(f"Execution attempt not found: {req_id}")
        return attempt

    def mark_dispatching(self, req_id: str) -> ExecutionAttempt:
        def _operation(session: Session) -> ExecutionAttempt:
            attempt = session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id).with_for_update()
            )
            if attempt is None:
                raise ExecutionAttemptNotFoundError(f"req_id {req_id} not found.")
            if attempt.status != ExecutionAttemptStatus.CREATED:
                raise ExecutionAttemptStateTransitionError(
                    f"req_id {req_id} cannot transition to DISPATCHING from {attempt.status.name}."
                )
            attempt.status = ExecutionAttemptStatus.DISPATCHING
            attempt.dispatch_started_at = self._utcnow()
            return attempt
        return self._run_in_new_transaction(_operation)

    def mark_accepted(self, req_id: str) -> ExecutionAttempt:
        def _operation(session: Session) -> ExecutionAttempt:
            attempt = session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id).with_for_update()
            )
            if attempt is None:
                raise ExecutionAttemptNotFoundError(f"req_id {req_id} not found.")
            if attempt.status not in (ExecutionAttemptStatus.CREATED, ExecutionAttemptStatus.DISPATCHING):
                raise ExecutionAttemptStateTransitionError(
                    f"Cannot transition from {attempt.status} to ACCEPTED"
                )
            attempt.status = ExecutionAttemptStatus.ACCEPTED
            attempt.goal_accepted_at = self._utcnow()
            return attempt
        return self._run_in_new_transaction(_operation)

    def apply_result(
        self,
        req_id: str,
        status: ExecutionAttemptStatus,
        result_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> ExecutionAttempt:
        if status not in (
            ExecutionAttemptStatus.SUCCEEDED,
            ExecutionAttemptStatus.FAILED,
            ExecutionAttemptStatus.CANCELED,
        ):
            raise ValueError("apply_result only accepts terminal states")

        def _operation(session: Session) -> ExecutionAttempt:
            attempt = session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id).with_for_update()
            )
            if attempt is None:
                raise ExecutionAttemptNotFoundError(f"Execution attempt not found: {req_id}")
            if attempt.status in (
                ExecutionAttemptStatus.SUCCEEDED,
                ExecutionAttemptStatus.FAILED,
                ExecutionAttemptStatus.CANCELED,
            ):
                return attempt
            attempt.status = status
            attempt.completed_at = self._utcnow()
            if result_payload is not None:
                attempt.result_payload_json = json.dumps(result_payload, sort_keys=True, separators=(",", ":"))
            attempt.error_code = error_code
            attempt.detail = detail
            if status is ExecutionAttemptStatus.FAILED:
                self._queue_error_notification(attempt)
            return attempt

        return self._run_in_new_transaction(_operation)

    def mark_unknown(self, req_id: str, detail: str | None = None) -> ExecutionAttempt:
        def _operation(session: Session) -> ExecutionAttempt:
            attempt = session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id).with_for_update()
            )
            if attempt is None:
                raise ExecutionAttemptNotFoundError(f"Execution attempt not found: {req_id}")
            if attempt.status in (
                ExecutionAttemptStatus.SUCCEEDED,
                ExecutionAttemptStatus.FAILED,
                ExecutionAttemptStatus.CANCELED,
            ):
                raise ExecutionAttemptStateTransitionError(
                    f"Cannot transition from {attempt.status} to UNKNOWN"
                )
            if attempt.status is ExecutionAttemptStatus.UNKNOWN:
                return attempt
            attempt.status = ExecutionAttemptStatus.UNKNOWN
            if detail:
                attempt.detail = detail
            self._queue_error_notification(attempt)
            return attempt

        return self._run_in_new_transaction(_operation)

    def record_synthetic_success_in_current_transaction(
        self,
        *,
        executor_type: ExecutorType,
        command_type: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
        job_id: int | None = None,
        job_step_id: int | None = None,
        job_delivery_id: int | None = None,
        req_id: str,
        detail: str | None = None,
    ) -> ExecutionAttempt:
        """Record a known synthetic success atomically in the caller's transaction.

        Normal Action dispatch deliberately persists CREATED and DISPATCHING before
        I/O. Controlled maintenance/recovery has no Action I/O and must not leave
        a partial Attempt if a later validation fails, so this method applies the
        same lifecycle timestamps and terminal-result representation without an
        intermediate commit. The caller owns the surrounding commit/rollback.
        """
        if not req_id.strip():
            raise ValueError("req_id must be non-empty.")
        canonical_payload = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        existing = self._session.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.req_id == req_id).with_for_update()
        )
        if existing is not None:
            if existing.request_payload_json != canonical_payload:
                raise ExecutionAttemptConflictError(
                    f"req_id {req_id} was already used with a different payload."
                )
            return existing

        attempt_no_query = select(func.max(ExecutionAttempt.attempt_no)).where(
            ExecutionAttempt.command_type == command_type
        )
        if job_step_id is not None:
            attempt_no_query = attempt_no_query.where(ExecutionAttempt.job_step_id == job_step_id)
        elif job_delivery_id is not None:
            attempt_no_query = attempt_no_query.where(
                ExecutionAttempt.job_delivery_id == job_delivery_id
            )
        attempt_no = (self._session.scalar(attempt_no_query) or 0) + 1
        now = self._utcnow()
        attempt = ExecutionAttempt(
            req_id=req_id,
            executor_type=executor_type,
            command_type=command_type,
            job_id=job_id,
            job_step_id=job_step_id,
            job_delivery_id=job_delivery_id,
            attempt_no=attempt_no,
            status=ExecutionAttemptStatus.CREATED,
            request_payload_json=canonical_payload,
            created_at=now,
        )
        self._session.add(attempt)
        self._session.flush()

        # Canonical logical lifecycle: CREATED -> DISPATCHING -> SUCCEEDED.
        # It remains one transaction because no physical adapter is invoked.
        attempt.status = ExecutionAttemptStatus.DISPATCHING
        attempt.dispatch_started_at = now
        attempt.status = ExecutionAttemptStatus.SUCCEEDED
        attempt.completed_at = now
        attempt.result_payload_json = json.dumps(result_payload, sort_keys=True, separators=(",", ":"))
        attempt.detail = detail
        self._session.flush()
        return attempt

    def get_pending_attempts(self) -> list[ExecutionAttempt]:
        return list(
            self._session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.status.in_([
                    ExecutionAttemptStatus.CREATED,
                    ExecutionAttemptStatus.DISPATCHING,
                    ExecutionAttemptStatus.ACCEPTED
                ]))
            )
        )
