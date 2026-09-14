"""Test-only, fail-closed advance of one authoritative production blocker."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.config import get_settings
from shared.models.factory import (
    EventType,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionInspectionResultCode,
    ProductionInspectionType,
    ProductionJob,
    ProductionJobControlState,
    StepStatus,
)
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.terminal_drop_recovery_service import (
    TerminalDropAlreadyRecovered,
    TerminalDropRecoveryError,
    TerminalDropRecoveryService,
)

from shared.services.test_override_transport_service import (
    TestOverrideTransportError,
    TestOverrideTransportService,
)
from shared.services.production_orchestration_service import (
    ProductionOrchestrationService,
    TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX,
)
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from fms_server.incoming_qa_v02_orchestration_service import (
    IncomingQAV02ActiveInspectionError,
    IncomingQAV02OrchestrationError,
    IncomingQAV02OrchestrationService,
)


logger = logging.getLogger(__name__)
_BENCHMARK_DATABASE = "smart_factory_benchmark"
_UNRESOLVED = (
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
)


class TestOverrideError(RuntimeError):
    pass


class TestOverrideDeniedError(TestOverrideError):
    pass


class TestOverrideStaleError(TestOverrideError):
    pass


class CurrentBlockerType(StrEnum):
    ROBOT_CELL_STEP_EXECUTION = "ROBOT_CELL_STEP_EXECUTION"
    MATERIAL_DELIVERY = "MATERIAL_DELIVERY"
    PRE_ROOF_INSPECTION = "PRE_ROOF_INSPECTION"
    INCOMING_QA_INSPECTION = "INCOMING_QA_INSPECTION"
    OPERATOR_EXECUTION_READY = "OPERATOR_EXECUTION_READY"
    MANUAL_PHYSICAL_READY = "MANUAL_PHYSICAL_READY"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class CurrentBlocker:
    blocker_type: CurrentBlockerType
    entity_id: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TestOverrideResult:
    advanced: CurrentBlocker
    next_blocker: CurrentBlocker | None


@dataclass(frozen=True, slots=True)
class TerminalDropCleanupResult:
    job_delivery_id: int
    attempt_id: int | None
    already_recovered: bool


TestOverrideError.__test__ = False
TestOverrideDeniedError.__test__ = False
TestOverrideStaleError.__test__ = False


class TestOverrideService:
    """Orchestration-only test seam; lifecycle services remain authoritative."""

    def __init__(
        self,
        session: Session,
        *,
        enabled: bool | None = None,
        database_name_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._enabled = get_settings().test_override_enabled if enabled is None else enabled
        self._database_name_resolver = database_name_resolver

    def require_capability(self) -> None:
        self._require_capability()

    def current_blocker(self, *, job_id: int) -> CurrentBlocker | None:
        job = self._job(job_id, lock=False)
        self._require_active_control(job)
        return self._resolve(job)

    def start_current_robot_cell_blocker(
        self,
        *,
        job_id: int,
        expected_blocker_type: CurrentBlockerType,
        expected_entity_id: int,
    ) -> TestOverrideResult:
        """Persist exactly one ready Robot Cell step as synthetic RUNNING.

        The normal lifecycle service owns inventory, timestamps, STEP_STARTED,
        and post-commit production.changed.  No ExecutionAttempt or Cell command
        is created by this test-only path.
        """
        self._require_capability()
        job = self._job(job_id, lock=True)
        self._require_active_control(job)
        current = self._resolve(job)
        if current is None:
            raise TestOverrideStaleError("No unique current production blocker can be started.")
        if current.blocker_type is not expected_blocker_type or current.entity_id != expected_entity_id:
            raise TestOverrideStaleError("Current blocker no longer matches the monitoring UI request.")
        if current.blocker_type is not CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION:
            raise TestOverrideDeniedError("Only a current Robot Cell execution blocker may use synthetic start.")
        step = self._session.get(JobStep, current.entity_id)
        if step is None or step.status is not StepStatus.PENDING:
            raise TestOverrideDeniedError("Synthetic start requires a current PENDING Robot Cell step.")
        self._require_no_unresolved_attempt(job_id=job.job_id)
        orchestration = ProductionOrchestrationService(self._session)
        self._start_requested_job_for_test_override(job=job, orchestration=orchestration)
        orchestration.start_step_for_test_override(current.entity_id)
        logger.warning("[TEST_OVERRIDE] job_id=%s ROBOT_CELL_STEP_EXECUTION step_id=%s synthetic RUNNING.", job.job_id, current.entity_id)
        refreshed = self._job(job.job_id, lock=False)
        return TestOverrideResult(advanced=current, next_blocker=self._resolve(refreshed))

    def recover_terminal_drop_for_test_override(
        self, *, job_id: int, job_delivery_id: int
    ) -> TerminalDropCleanupResult:
        """Benchmark-only cleanup for a terminal Job that still durably owns DROP.

        This deliberately records canonical successful EMPTY_RETURN evidence;
        it never toggles DROP state or invokes a TurtleBot adapter.
        """
        self._require_capability()
        job = self._job(job_id, lock=True)
        self._require_active_control(job)
        try:
            result = TerminalDropRecoveryService(self._session).recover(
                job_id=job.job_id, delivery_id=job_delivery_id
            )
        except TerminalDropAlreadyRecovered:
            return TerminalDropCleanupResult(
                job_delivery_id=job_delivery_id, attempt_id=None, already_recovered=True
            )
        except TerminalDropRecoveryError as exc:
            raise TestOverrideDeniedError(str(exc)) from exc
        return TerminalDropCleanupResult(
            job_delivery_id=job_delivery_id,
            attempt_id=result.attempt_id,
            already_recovered=False,
        )

    def complete_empty_pallet_return_for_test_override(
        self, *, job_id: int, job_delivery_id: int
    ) -> ExecutionAttempt:
        """Bounded no-I/O EMPTY_RETURN seam for an already consumed DROP owner."""
        self._require_capability()
        job = self._job(job_id, lock=True)
        self._require_active_control(job)
        try:
            return TestOverrideTransportService(self._session).complete_empty_pallet_return(
                production_job_id=job.job_id,
                job_delivery_id=job_delivery_id,
            )
        except TestOverrideTransportError as exc:
            raise TestOverrideDeniedError(str(exc)) from exc

    def complete_house_outbound_for_test_override(self, *, job_id: int) -> ProductionJob:
        """Complete the explicit post-roof demo checkpoint without robot I/O."""
        self._require_capability()
        job = self._job(job_id, lock=True)
        self._require_active_control(job)
        if job.status is JobStatus.COMPLETED:
            return job
        if job.status not in {JobStatus.RUNNING, JobStatus.ROOF_READY}:
            raise TestOverrideDeniedError("House outbound completion requires a RUNNING production job.")
        self._require_no_unresolved_attempt(job_id=job.job_id)
        roof = self._session.scalar(select(JobStep).where(
            JobStep.job_id == job.job_id,
            JobStep.operation_code == "INSTALL_ROOF",
            JobStep.status == StepStatus.COMPLETED,
        ).limit(1))
        if roof is None:
            raise TestOverrideDeniedError("House outbound completion requires a completed ROOF JobStep.")
        inspection = self._latest_inspection(job.job_id)
        if inspection is None or inspection.status is not ProductionInspectionStatus.COMPLETED or inspection.result is not ProductionInspectionResultCode.PASS:
            raise TestOverrideDeniedError("House outbound completion requires a released PRE_ROOF inspection.")
        return ProductionOrchestrationService(self._session).complete_job(job.job_id)

    def advance(
        self,
        *,
        job_id: int,
        expected_blocker_type: CurrentBlockerType,
        expected_entity_id: int,
    ) -> TestOverrideResult:
        self._require_capability()
        job = self._job(job_id, lock=True)
        self._require_active_control(job)
        current = self._resolve(job)
        if current is None:
            raise TestOverrideStaleError("No unique current production blocker can be advanced.")
        if current.blocker_type is not expected_blocker_type or current.entity_id != expected_entity_id:
            raise TestOverrideStaleError("Current blocker no longer matches the monitoring UI request.")
        self._require_no_unresolved_attempt(job_id=job.job_id)

        if current.blocker_type is CurrentBlockerType.INCOMING_QA_INSPECTION:
            try:
                transaction = IncomingQAV02OrchestrationService(self._session).complete_current_mode_for_test_override(
                    job_id=job.job_id
                )
            except (IncomingQAV02ActiveInspectionError, IncomingQAV02OrchestrationError) as exc:
                raise TestOverrideDeniedError(str(exc)) from exc
            logger.warning(
                "[TEST_OVERRIDE] job_id=%s INCOMING_QA_INSPECTION mode=%s transaction_id=%s passed.",
                job.job_id, transaction.inspection_mode, transaction.transaction_id,
            )
        elif current.blocker_type is CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION:
            step = self._session.get(JobStep, current.entity_id)
            if step is None or step.job_id != job.job_id:
                raise TestOverrideStaleError("Current Robot Cell step disappeared.")
            orchestration = ProductionOrchestrationService(self._session)
            if step.status is StepStatus.PENDING:
                # Existing one-click compatibility: synthetic start followed by completion.
                self._start_requested_job_for_test_override(job=job, orchestration=orchestration)
                orchestration.start_step_after_execution_accepted(current.entity_id)
            elif step.status is StepStatus.RUNNING:
                if not self._is_synthetic_running_step(job_step_id=step.job_step_id):
                    raise TestOverrideDeniedError("A real or unverified Robot Cell RUNNING step cannot be synthetically completed.")
            else:
                raise TestOverrideStaleError("Current Robot Cell step is no longer completable.")
            orchestration.complete_step(current.entity_id)
            self._mark_existing_event(job.job_id, current.entity_id, EventType.STEP_COMPLETED, current)
        elif current.blocker_type is CurrentBlockerType.MATERIAL_DELIVERY:
            delivery = self._session.get(JobMaterialDelivery, current.entity_id)
            if delivery is None or delivery.production_job_id != job.job_id:
                raise TestOverrideStaleError("Current delivery disappeared.")
            if delivery.status is not MaterialDeliveryStatus.PENDING:
                raise TestOverrideStaleError("Delivery is no longer pending.")
            try:
                TestOverrideTransportService(self._session).complete_pending_delivery(
                    production_job_id=job.job_id,
                    job_delivery_id=delivery.job_delivery_id,
                )
            except TestOverrideTransportError as exc:
                raise TestOverrideDeniedError(str(exc)) from exc
            logger.warning("[TEST_OVERRIDE] job_id=%s MATERIAL_DELIVERY entity_id=%s completed.", job.job_id, current.entity_id)
        elif current.blocker_type is CurrentBlockerType.PRE_ROOF_INSPECTION:
            completion = ProductionCompletionService(self._session)
            inspection = self._latest_inspection(job.job_id)
            if inspection is not None and (
                inspection.status is ProductionInspectionStatus.RUNNING
                and inspection.wire_request_snapshot_json is not None
            ):
                raise TestOverrideDeniedError("A real PRE_ROOF Vision inspection is in flight.")
            if inspection is None or inspection.status is not ProductionInspectionStatus.RUNNING:
                completion.start_pre_roof_inspection(production_job_id=job.job_id)
            completion.pass_pre_roof_inspection(production_job_id=job.job_id)
            self._mark_existing_event(job.job_id, None, EventType.INSPECTION_PASSED, current)
        else:
            raise TestOverrideDeniedError(
                f"{current.blocker_type.value} must use its existing operator command, not synthetic completion."
            )

        refreshed = self._job(job.job_id, lock=False)
        return TestOverrideResult(advanced=current, next_blocker=self._resolve(refreshed))

    def _require_capability(self) -> None:
        if not self._enabled:
            raise TestOverrideDeniedError("TEST_OVERRIDE_ENABLED is false.")
        if self._database_name() != _BENCHMARK_DATABASE:
            raise TestOverrideDeniedError("Test Override requires a proven smart_factory_benchmark database bind.")

    def _database_name(self) -> str:
        if self._database_name_resolver is not None:
            return self._database_name_resolver()
        bind = self._session.get_bind()
        if bind.dialect.name != "postgresql":
            raise TestOverrideDeniedError("Test Override requires a PostgreSQL benchmark bind.")
        try:
            configured = make_url(str(bind.url)).database
            if configured != _BENCHMARK_DATABASE:
                raise TestOverrideDeniedError("Test Override database URL is not smart_factory_benchmark.")
            return str(self._session.execute(text("SELECT current_database()")).scalar_one())
        except TestOverrideDeniedError:
            raise
        except Exception as exc:
            raise TestOverrideDeniedError("Test Override could not prove database identity.") from exc

    def _job(self, job_id: int, *, lock: bool) -> ProductionJob:
        statement = select(ProductionJob).where(ProductionJob.job_id == job_id)
        if lock:
            statement = statement.with_for_update()
        job = self._session.scalar(statement)
        if job is None:
            raise TestOverrideStaleError(f"Production job not found: job_id={job_id}.")
        return job

    @staticmethod
    def _require_active_control(job: ProductionJob) -> None:
        if job.control_state is not ProductionJobControlState.ACTIVE:
            raise TestOverrideDeniedError("Test Override is blocked while production pause/resume control is not ACTIVE.")

    def _resolve(self, job: ProductionJob) -> CurrentBlocker | None:
        qa_blocker = self._incoming_qa_blocker(job)
        if qa_blocker is not None:
            return qa_blocker
        if job.status is JobStatus.PRE_ROOF_READY:
            inspection = self._latest_inspection(job.job_id)
            return CurrentBlocker(
                CurrentBlockerType.PRE_ROOF_INSPECTION,
                inspection.inspection_id if inspection is not None else job.job_id,
            )
        # Normally the FMS worker owns REQUESTED -> RUNNING before dispatching
        # the first step. In TEST_OVERRIDE mode that worker can intentionally
        # be off for a segmented demo, so expose an otherwise-ready first step
        # as the same bounded Robot Cell blocker. The start/advance actions
        # then use ProductionOrchestrationService.start_job() before changing
        # the step; no direct Job status mutation is introduced here.
        if job.status not in {JobStatus.REQUESTED, JobStatus.RUNNING, JobStatus.ROOF_READY}:
            return None
        step = ProductionOrchestrationService(self._session).get_next_step(job.job_id)
        if step is None:
            return None
        if step.status is StepStatus.RUNNING:
            return (
                CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id, "Synthetic RUNNING step awaiting operator completion.")
                if self._is_synthetic_running_step(job_step_id=step.job_step_id)
                else None
            )
        if step.status is not StepStatus.PENDING:
            return None
        readiness = StepReadinessService(MaterialDeliveryService(self._session)).evaluate(
            job_id=job.job_id, job_step_id=step.job_step_id
        )
        if readiness.ready:
            return CurrentBlocker(CurrentBlockerType.ROBOT_CELL_STEP_EXECUTION, step.job_step_id)
        deliveries = MaterialDeliveryService(self._session).get_required_deliveries_for_step(step.job_step_id)
        if readiness.reason is StepReadinessReason.TRANSPORT_PENDING:
            pending = next((item for item in deliveries if item.status is MaterialDeliveryStatus.PENDING), None)
            return CurrentBlocker(CurrentBlockerType.MATERIAL_DELIVERY, pending.job_delivery_id) if pending else None
        if readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED:
            return CurrentBlocker(CurrentBlockerType.OPERATOR_EXECUTION_READY, step.job_step_id)
        if readiness.reason in {
            StepReadinessReason.PHYSICAL_READY_REQUIRED,
            StepReadinessReason.MANUAL_PRESTAGE_REQUIRED,
        }:
            delivery = deliveries[0] if len(deliveries) == 1 else None
            return CurrentBlocker(CurrentBlockerType.MANUAL_PHYSICAL_READY, delivery.job_delivery_id) if delivery else None
        return CurrentBlocker(CurrentBlockerType.UNSUPPORTED, step.job_step_id, readiness.reason.value if readiness.reason else None)

    @staticmethod
    def _start_requested_job_for_test_override(
        *, job: ProductionJob, orchestration: ProductionOrchestrationService
    ) -> None:
        """Reuse the normal start transition when FMS is intentionally off.

        This is a no-op for RUNNING/ROOF_READY jobs, preserving established
        Test Override behavior after the first step. The orchestration service
        owns the durable JOB_STARTED event and post-commit notification.
        """
        if job.status is JobStatus.REQUESTED:
            orchestration.start_job(job.job_id)

    def _incoming_qa_blocker(self, job: ProductionJob) -> CurrentBlocker | None:
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            return None
        if self._session.scalar(
            select(JobStep.job_step_id).where(
                JobStep.job_id == job.job_id, JobStep.status != StepStatus.PENDING
            ).limit(1)
        ) is not None:
            return None
        # A generic test fixture or non-QA product may have delivery items but
        # no complete, canonical v0.2 expected-material set.  Such a job must
        # retain its pre-existing step/delivery Test Override behavior rather
        # than being misidentified as an Incoming QA blocker.
        try:
            IncomingQAV02OrchestrationService(self._session)._locked_job_and_expected_items(job.job_id)
        except IncomingQAV02OrchestrationError:
            return None
        readiness = IncomingQAOrchestrationService(self._session).preproduction_readiness(job_id=job.job_id)
        if readiness.qa_passed:
            return None
        return CurrentBlocker(CurrentBlockerType.INCOMING_QA_INSPECTION, job.job_id, "Current required Incoming QA mode")

    def _is_synthetic_running_step(self, *, job_step_id: int) -> bool:
        return self._session.scalar(
            select(ProductionEvent.event_id)
            .where(
                ProductionEvent.job_step_id == job_step_id,
                ProductionEvent.event_type == EventType.STEP_STARTED,
                ProductionEvent.message.like(f"{TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX}%"),
            )
            .order_by(ProductionEvent.event_id.desc())
            .limit(1)
        ) is not None

    def _require_no_unresolved_attempt(self, *, job_id: int) -> None:
        active = self._session.scalar(
            select(ExecutionAttempt.attempt_id)
            .where(ExecutionAttempt.job_id == job_id, ExecutionAttempt.status.in_(_UNRESOLVED))
            .with_for_update()
            .limit(1)
        )
        if active is not None:
            raise TestOverrideDeniedError("A physical ExecutionAttempt is unresolved; synthetic completion is blocked.")

    def _latest_inspection(self, job_id: int) -> ProductionInspection | None:
        return self._session.scalar(
            select(ProductionInspection)
            .where(
                ProductionInspection.production_job_id == job_id,
                ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
            )
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )

    def _mark_existing_event(
        self, job_id: int, step_id: int | None, event_type: EventType, blocker: CurrentBlocker
    ) -> None:
        event = self._session.scalar(
            select(ProductionEvent)
            .where(
                ProductionEvent.job_id == job_id,
                ProductionEvent.job_step_id == step_id,
                ProductionEvent.event_type == event_type,
            )
            .order_by(ProductionEvent.event_id.desc())
            .limit(1)
        )
        if event is not None:
            event.message = f"[TEST_OVERRIDE] blocker={blocker.blocker_type.value} entity_id={blocker.entity_id}; {event.message}"
            self._session.commit()
TestOverrideService.__test__ = False
