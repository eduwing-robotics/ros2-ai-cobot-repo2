import logging
import uuid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import MaterialPrefetchMode
from shared.models.factory import (
    JobStatus,
    ProductionJob,
    MaterialDeliveryStatus,
    JobMaterialDelivery,
    JobMaterialFeedExecution,
    MaterialFeedStatus,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    ProductionJobControlState,
    StepStatus,
    EventType,
    ProductionEvent,
)
from shared.services.production_orchestration_service import (
    ProductionOrchestrationService,
    TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX,
)
from shared.services.step_readiness_service import StepReadinessService, StepReadinessReason
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.inventory_reservation_service import InventoryReservationService
from shared.services.transport_eligibility_service import TransportEligibilityService

from fms_server.execution_coordinator import FmsExecutionCoordinator, CoordinatorOutcome
from shared.services.production_cell_payload_builder import ProductionCellPayloadBuilder
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.forklift_action_adapter import ForkliftExecutionResult
from fms_server.transport_location_resolver import (
    TransportLocationResolutionError,
    resolve_legacy_transport_locations,
    resolve_policy_transport_locations,
)
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import CellTaskExecutionResult, RobotCellActionAdapter
from fms_server.automatic_empty_pallet_return_service import AutomaticEmptyPalletReturnService
from fms_server.automatic_return_home_service import AutomaticReturnHomeService
from fms_server.material_prefetch_service import MaterialPrefetchService

logger = logging.getLogger(__name__)

class FmsWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        fms_execution_coordinator_factory: Callable[[Session], FmsExecutionCoordinator],
        forklift_execution_coordinator_factory: Callable[[Session], ForkliftExecutionCoordinator],
        material_feed_execution_coordinator_factory: Callable[[Session], MaterialFeedExecutionCoordinator],
        pause_resume_coordinator_factory: Callable[[Session], object] | None = None,
        auto_empty_pallet_return_enabled: bool = False,
        material_prefetch_mode: MaterialPrefetchMode | str = MaterialPrefetchMode.DISABLED,
    ):
        self._session_factory = session_factory
        self._fms_execution_coordinator_factory = fms_execution_coordinator_factory
        self._forklift_execution_coordinator_factory = forklift_execution_coordinator_factory
        self._material_feed_execution_coordinator_factory = material_feed_execution_coordinator_factory
        self._pause_resume_coordinator_factory = pause_resume_coordinator_factory
        # Fail closed by default. FMS main enables this only while its fake
        # TurtleBot adapter is selected; a real unfinished runtime must never
        # record a fake successful cleanup.
        self._auto_empty_pallet_return_enabled = auto_empty_pallet_return_enabled
        self._material_prefetch_mode = MaterialPrefetchMode(material_prefetch_mode)

    def set_material_prefetch_mode(self, mode: MaterialPrefetchMode | str) -> None:
        """Set the explicit logistics scheduler policy without touching JobStep order."""
        self._material_prefetch_mode = MaterialPrefetchMode(mode)

    def set_auto_empty_pallet_return_enabled(self, enabled: bool) -> None:
        """Configure the FMS runtime capability after construction.

        Keeping this separate preserves the existing factory constructor shape
        used by integrations while allowing main to fail closed in real mode.
        """
        self._auto_empty_pallet_return_enabled = enabled

    def tick(self) -> bool:
        """Run one iteration of the FMS dispatch loop. Returns True if any dispatch occurred."""
        try:
            with self._session_factory() as session:
                if self._pause_resume_coordinator_factory is not None:
                    reconciler = self._pause_resume_coordinator_factory(session)
                    if reconciler.reconcile_once():
                        return True
                return self._tick_in_session(session)
        except Exception:
            logger.exception("FMS Worker encountered an error during tick.")
            return False

    def _tick_in_session(self, session: Session) -> bool:
        orchestration = ProductionOrchestrationService(session)
        delivery_service = MaterialDeliveryService(session)
        readiness_service = StepReadinessService(delivery_service)
        feed_service = MaterialFeedExecutionService(session)
        transport_eligibility_service = TransportEligibilityService(session)

        jobs = session.scalars(
            select(ProductionJob)
            .where(ProductionJob.status.in_([
                JobStatus.REQUESTED,
                JobStatus.READY,
                JobStatus.RUNNING,
                JobStatus.PRE_ROOF_READY,
                JobStatus.ROOF_READY
            ]))
            .order_by(ProductionJob.job_id.asc())
        ).all()

        dispatched_anything = False

        # Durable reconciliation pass: also covers a crash after the Robot
        # Step COMPLETED commit but before its immediate post-commit trigger.
        if self._try_dispatch_automatic_empty_return(session):
            dispatched_anything = True
        # A completed Empty Return can be followed by a crash before HOME is
        # sent. This independent durable reconciliation runs on every tick.
        if self._try_dispatch_automatic_return_home(session):
            dispatched_anything = True

        if self._material_prefetch_mode is MaterialPrefetchMode.ONE_AHEAD:
            # Starting a Job is a local state transition, not a physical action.
            # Do it before evaluating the execution frontier for future material.
            for job in jobs:
                if job.control_state is not ProductionJobControlState.ACTIVE:
                    continue
                if job.status in [JobStatus.REQUESTED, JobStatus.READY]:
                    orchestration.start_job(job.job_id)
                    session.commit()
                    logger.info("Started job %s", job.job_code)
                    dispatched_anything = True

            # At most one physical prefetch is dispatched per Worker tick. The
            # following production pass runs on a later tick after a successful
            # prefetch, allowing Robot Cell and Forklift work to overlap without
            # issuing both network actions from this transaction boundary.
            for job in jobs:
                if job.control_state is not ProductionJobControlState.ACTIVE:
                    continue
                if self._try_dispatch_one_ahead_prefetch(
                    session=session,
                    job=job,
                    orchestration=orchestration,
                    delivery_service=delivery_service,
                    transport_eligibility_service=transport_eligibility_service,
                ):
                    return True

        for job in jobs:
            if job.control_state is not ProductionJobControlState.ACTIVE:
                continue
            if job.status in [JobStatus.REQUESTED, JobStatus.READY]:
                orchestration.start_job(job.job_id)
                session.commit()
                logger.info("Started job %s", job.job_code)
                dispatched_anything = True

            if job.status is JobStatus.PRE_ROOF_READY:
                # PRE_ROOF still needs an authoritative quality outcome. Its
                # runtime Roof JobStep does not exist yet, so there is no Robot
                # task to generate here.
                continue

            step = orchestration.get_next_step(job.job_id)
            if not step:
                continue

            if step.status == StepStatus.RUNNING:
                # A fresh FMS process has no in-memory Cell execution handle.
                # Reconcile only durable terminal evidence; an ambiguous attempt
                # must fail closed instead of physically replaying the task.
                if self._recover_running_robot_step(
                    session=session,
                    orchestration=orchestration,
                    job=job,
                    step=step,
                ):
                    dispatched_anything = True
                continue

            # An accepted Robot Cell Goal is durable evidence that physical
            # material issue may already have begun, even if a crash happened
            # before the normal accepted -> inventory issue -> RUNNING
            # transaction completed. Reconcile the issue first and never
            # redispatch this ambiguous Step from PENDING.
            accepted_attempt = session.scalar(
                select(ExecutionAttempt)
                .where(
                    ExecutionAttempt.job_step_id == step.job_step_id,
                    ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
                    ExecutionAttempt.goal_accepted_at.is_not(None),
                )
                .order_by(ExecutionAttempt.attempt_no.desc(), ExecutionAttempt.attempt_id.desc())
                .limit(1)
            )
            if accepted_attempt is not None:
                if self._recover_accepted_inventory_issue(
                    session=session,
                    job=job,
                    step=step,
                    attempt=accepted_attempt,
                ):
                    dispatched_anything = True
                logger.warning(
                    "Skipping Robot Cell redispatch for Job %s Step %s after durable accepted attempt %s; manual execution reconciliation remains required.",
                    job.job_code,
                    step.job_step_id,
                    accepted_attempt.req_id,
                )
                continue

            # NEW AUDIT RULE: Avoid Redispatching if there is already an unresolved attempt for this Step.
            active_attempt = session.scalar(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.job_step_id == step.job_step_id, ExecutionAttempt.status.in_([
                    ExecutionAttemptStatus.CREATED,
                    ExecutionAttemptStatus.DISPATCHING,
                    ExecutionAttemptStatus.ACCEPTED,
                    ExecutionAttemptStatus.UNKNOWN
                ]))
            )
            if active_attempt:
                logger.warning("Skipping Robot Cell dispatch for Job %s Step %s due to unresolved attempt %s", job.job_code, step.job_step_id, active_attempt.req_id)
                continue

            readiness = readiness_service.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)
            if not readiness.ready:
                if readiness.reason in (
                    StepReadinessReason.MATERIAL_NOT_READY,
                    StepReadinessReason.TRANSPORT_PENDING,
                ):
                    # Dispatch Material Delivery if needed
                    dispatched = self._try_dispatch_forklift(session, step.job_step_id, delivery_service, transport_eligibility_service)
                    if dispatched: dispatched_anything = True
                elif readiness.reason == StepReadinessReason.MATERIAL_FEED_NOT_READY:
                    # Dispatch Material Feed if needed
                    dispatched = self._try_dispatch_feed(session, step.job_step_id, delivery_service, feed_service)
                    if dispatched: dispatched_anything = True
                continue

            # Ready for Robot Cell dispatch
            # generate temporary req_id for this dispatch
            req_id = str(uuid.uuid4())
            fms_coordinator = self._fms_execution_coordinator_factory(session)

            logger.info("Dispatching Robot Cell Action for Job %s Step %s", job.job_code, step.job_step_id)
            try:
                result = fms_coordinator.execute_step(
                    job_id=job.job_id,
                    job_step_id=step.job_step_id,
                    parts_json=RobotCellActionAdapter.serialize_parts_payload(ProductionCellPayloadBuilder(session).build_for_job_step(job_id=job.job_id, job_step_id=step.job_step_id).parts),
                    req_id=req_id
                )
                session.commit()
                logger.info("Dispatch outcome: %s", result.outcome)
                dispatched_anything = True
                if result.outcome is CoordinatorOutcome.COMPLETED:
                    # The completed Step is now durable. Cleanup is a separate
                    # transaction/lifecycle; a return failure never rolls this
                    # Robot Cell success back.
                    if self._try_dispatch_automatic_empty_return(session):
                        dispatched_anything = True
                    # ReturnHome is separate from Step/Job completion. This
                    # post-commit fast path is duplicated by tick-start
                    # reconciliation for restart safety.
                    if self._try_dispatch_automatic_return_home(session):
                        dispatched_anything = True
            except Exception:
                logger.exception("Failed to dispatch cell action")
                session.rollback()

        return dispatched_anything

    @staticmethod
    def _recover_accepted_inventory_issue(
        *,
        session: Session,
        job: ProductionJob,
        step,
        attempt: ExecutionAttempt,
    ) -> bool:
        """Close the accepted-before-issue crash window exactly once.

        This does not infer a Cell result or alter the PENDING/RUNNING
        lifecycle. A process that lost its Action handle still needs normal
        manual outcome reconciliation, but stock cannot be released as if the
        accepted physical command had never happened.
        """
        if attempt.goal_accepted_at is None or step.inventory_consumed_at is not None:
            return False
        try:
            consumed = InventoryReservationService(session).consume_step_after_execution_accepted(
                job_step_id=step.job_step_id
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "Failed to recover accepted-but-unconsumed inventory issue job=%s step=%s req_id=%s",
                job.job_code,
                step.job_step_id,
                attempt.req_id,
            )
            return False
        if consumed:
            logger.warning(
                "Recovered accepted-but-unconsumed inventory issue job=%s step=%s req_id=%s",
                job.job_code,
                step.job_step_id,
                attempt.req_id,
            )
        return consumed

    def _recover_running_robot_step(
        self,
        *,
        session: Session,
        orchestration: ProductionOrchestrationService,
        job: ProductionJob,
        step,
    ) -> bool:
        """Recover a RUNNING Cell step only from durable Attempt evidence.

        No Cell status-query/resume contract exists.  Therefore any active or
        unknown attempt is ambiguous after an FMS restart and becomes an
        explicit failed JobStep rather than a replay candidate.
        """
        attempt = session.scalar(
            select(ExecutionAttempt)
            .where(
                ExecutionAttempt.job_step_id == step.job_step_id,
                ExecutionAttempt.executor_type == ExecutorType.ROBOT_CELL,
            )
            .order_by(ExecutionAttempt.attempt_no.desc(), ExecutionAttempt.attempt_id.desc())
            .limit(1)
        )
        if attempt is None:
            if self._is_test_override_running_step(session=session, job_step_id=step.job_step_id):
                logger.info("Leaving synthetic TEST_OVERRIDE RUNNING step held job=%s step=%s", job.job_code, step.job_step_id)
                return False
            return self._fail_running_step_recovery(
                orchestration=orchestration,
                job=job,
                step=step,
                reason="FMS restart found RUNNING step without a Robot Cell execution attempt; manual recovery is required.",
                error_code="FMS_RESTART_MISSING_ATTEMPT",
            )

        if attempt.status is ExecutionAttemptStatus.SUCCEEDED:
            # A durable validated Cell success may have committed before the
            # JobStep completion transaction was interrupted.
            orchestration.complete_step(step.job_step_id)
            session.expire_all()
            logger.warning(
                "Reconciled RUNNING step from succeeded Cell attempt job=%s step=%s req_id=%s",
                job.job_code,
                step.job_step_id,
                attempt.req_id,
            )
            return True

        if attempt.status is ExecutionAttemptStatus.FAILED:
            return self._fail_running_step_recovery(
                orchestration=orchestration,
                job=job,
                step=step,
                reason=attempt.detail or "Persisted Robot Cell execution attempt failed before FMS completed step cleanup.",
                error_code=attempt.error_code or "CELL_ATTEMPT_FAILED",
            )

        if attempt.status is ExecutionAttemptStatus.CANCELED:
            return self._fail_running_step_recovery(
                orchestration=orchestration,
                job=job,
                step=step,
                reason="Persisted Robot Cell execution attempt was canceled; manual recovery is required.",
                error_code="FMS_RESTART_CANCELED_ATTEMPT",
            )

        # CREATED/DISPATCHING/ACCEPTED/UNKNOWN cannot certify that the Cell did
        # not receive or complete the physical task. Record that ambiguity and
        # fail the existing lifecycle rather than creating a new dispatch.
        if attempt.status is not ExecutionAttemptStatus.UNKNOWN:
            ExecutionAttemptService(session).mark_unknown(
                attempt.req_id,
                detail="FMS restart lost the in-memory Robot Cell execution handle.",
            )
        return self._fail_running_step_recovery(
            orchestration=orchestration,
            job=job,
            step=step,
            reason="FMS restart cannot verify Robot Cell task outcome; manual recovery is required.",
            error_code="FMS_RESTART_UNCERTAIN_ATTEMPT",
        )

    def _is_test_override_running_step(self, *, session: Session, job_step_id: int) -> bool:
        return session.scalar(
            select(ProductionEvent.event_id)
            .where(
                ProductionEvent.job_step_id == job_step_id,
                ProductionEvent.event_type == EventType.STEP_STARTED,
                ProductionEvent.message.like(f"{TEST_OVERRIDE_STEP_STARTED_EVENT_PREFIX}%"),
            )
            .order_by(ProductionEvent.event_id.desc())
            .limit(1)
        ) is not None

    @staticmethod
    def _fail_running_step_recovery(
        *,
        orchestration: ProductionOrchestrationService,
        job: ProductionJob,
        step,
        reason: str,
        error_code: str,
    ) -> bool:
        orchestration.fail_step(step.job_step_id, reason=reason, error_code=error_code)
        logger.error(
            "Fail-closed RUNNING step recovery job=%s step=%s reason=%s",
            job.job_code,
            step.job_step_id,
            error_code,
        )
        return True

    def _try_dispatch_automatic_empty_return(self, session: Session) -> bool:
        return AutomaticEmptyPalletReturnService(
            session,
            forklift_execution_coordinator_factory=self._forklift_execution_coordinator_factory,
            runtime_enabled=self._auto_empty_pallet_return_enabled,
        ).dispatch_one_eligible_return()

    def _try_dispatch_automatic_return_home(self, session: Session) -> bool:
        return AutomaticReturnHomeService(
            session,
            forklift_execution_coordinator_factory=self._forklift_execution_coordinator_factory,
            # The same capability gate protects both fake-only TurtleBot
            # cleanup movements until the real runtime is available.
            runtime_enabled=self._auto_empty_pallet_return_enabled,
        ).dispatch_one_eligible_return_home()

    def _try_dispatch_one_ahead_prefetch(
        self,
        *,
        session: Session,
        job: ProductionJob,
        orchestration: ProductionOrchestrationService,
        delivery_service: MaterialDeliveryService,
        transport_eligibility_service: TransportEligibilityService,
    ) -> bool:
        """Dispatch one nearest future TRANSPORTED Delivery, if durable policy permits."""
        if job.status is not JobStatus.RUNNING:
            return False
        frontier = orchestration.get_next_step(job.job_id)
        if frontier is None:
            return False
        candidate = MaterialPrefetchService(session).nearest_future_transported_delivery(
            job_id=job.job_id,
            execution_frontier_step_order=frontier.resolved_step_order,
        )
        if candidate is None:
            return False
        delivery = session.get(JobMaterialDelivery, candidate.job_delivery_id)
        if delivery is None:
            return False
        logger.info(
            "Evaluating one-ahead material prefetch job_id=%s delivery_id=%s future_step_order=%s",
            job.job_id,
            delivery.job_delivery_id,
            candidate.earliest_consuming_step_order,
        )
        return self._try_dispatch_policy_forklift_delivery(
            session=session,
            delivery=delivery,
            delivery_service=delivery_service,
            transport_eligibility_service=transport_eligibility_service,
        )

    def _try_dispatch_forklift(
        self,
        session: Session,
        job_step_id: int,
        delivery_service: MaterialDeliveryService,
        transport_eligibility_service: TransportEligibilityService,
    ) -> bool:
        deliveries = delivery_service.get_required_deliveries_for_step(job_step_id)
        for delivery in deliveries:
            if self._is_legacy_delivery(delivery):
                if self._dispatch_legacy_forklift(session, delivery, delivery_service):
                    return True
                continue
            if self._try_dispatch_policy_forklift_delivery(
                session=session,
                delivery=delivery,
                delivery_service=delivery_service,
                transport_eligibility_service=transport_eligibility_service,
            ):
                return True
        return False

    def _try_dispatch_policy_forklift_delivery(
        self,
        *,
        session: Session,
        delivery: JobMaterialDelivery,
        delivery_service: MaterialDeliveryService,
        transport_eligibility_service: TransportEligibilityService,
    ) -> bool:
        try:
            payload = self._transport_payload(delivery)
        except TransportLocationResolutionError as exc:
            logger.error(
                "Policy transport location resolution failed delivery_id=%s: %s",
                delivery.job_delivery_id,
                exc,
            )
            return False
        claim = transport_eligibility_service.claim_transport(
            job_delivery_id=delivery.job_delivery_id,
            request_payload=payload,
        )
        if not claim.eligible:
            logger.info(
                "Skipping policy transport delivery_id=%s reason=%s",
                delivery.job_delivery_id,
                claim.reason.value,
            )
            return False

        # Claim and correlation are committed before the adapter can send.
        logger.info(
            "Dispatching claimed policy transport delivery_id=%s req_id=%s",
            delivery.job_delivery_id,
            claim.request_id,
        )
        forklift_coord = self._forklift_execution_coordinator_factory(session)
        try:
            result = forklift_coord.execute_transport(
                job_id=delivery.production_job_id,
                delivery_id=delivery.job_delivery_id,
                pickup_code=payload["pickup_code"],
                dropoff_code=payload["dropoff_code"],
                req_id=claim.request_id,
                attempt_preclaimed=True,
            )
            if result.status.value == "SUCCEEDED":
                delivery_service.complete_delivery(delivery.job_delivery_id)
            else:
                transport_eligibility_service.fail_claimed_transport(
                    job_delivery_id=delivery.job_delivery_id,
                    request_id=claim.request_id or "",
                    reason=(result.detail or "Transport dispatch failed"),
                )
            return True
        except Exception as exc:
            logger.exception(
                "Claimed transport dispatch failed delivery_id=%s",
                delivery.job_delivery_id,
            )
            transport_eligibility_service.fail_claimed_transport(
                job_delivery_id=delivery.job_delivery_id,
                request_id=claim.request_id or "",
                reason=f"Transport dispatch exception: {exc}",
            )
            return False


    @staticmethod
    def _is_legacy_delivery(delivery: JobMaterialDelivery) -> bool:
        return (
            delivery.supply_mode is None
            and delivery.supply_group_code is None
            and delivery.supply_destination_code is None
        )


    @staticmethod
    def _transport_payload(delivery: JobMaterialDelivery) -> dict:
        locations = resolve_policy_transport_locations(delivery)
        return {
            "job_id": delivery.production_job_id,
            "delivery_id": delivery.job_delivery_id,
            "pickup_code": locations.pickup_code,
            "dropoff_code": locations.dropoff_code,
        }

    def _dispatch_legacy_forklift(
        self,
        session: Session,
        delivery: JobMaterialDelivery,
        delivery_service: MaterialDeliveryService,
    ) -> bool:
        if delivery.status is not MaterialDeliveryStatus.PENDING:
            return False
        logger.info("Dispatching legacy Forklift Delivery %s", delivery.job_delivery_id)
        forklift_coord = self._forklift_execution_coordinator_factory(session)
        req_id = str(uuid.uuid4())
        try:
            locations = resolve_legacy_transport_locations(delivery)
            result = forklift_coord.execute_transport(
                job_id=delivery.production_job_id,
                delivery_id=delivery.job_delivery_id,
                pickup_code=locations.pickup_code,
                dropoff_code=locations.dropoff_code,
                req_id=req_id,
            )
            delivery_service.start_delivery(delivery.job_delivery_id)
            if result.status.value == "SUCCEEDED":
                delivery_service.complete_delivery(delivery.job_delivery_id)
            elif result.status.value == "FAILED":
                delivery_service.fail_delivery(delivery.job_delivery_id, "Forklift delivery failed")
            session.commit()
            return True
        except Exception:
            logger.exception("Failed to dispatch legacy forklift action")
            session.rollback()
            return False

    def _try_dispatch_feed(
        self,
        session: Session,
        job_step_id: int,
        delivery_service: MaterialDeliveryService,
        feed_service: MaterialFeedExecutionService
    ) -> bool:
        deliveries = delivery_service.get_required_deliveries_for_step(job_step_id)
        # Phase 3A: Feed remains an explicit legacy-only lifecycle even when
        # historical policy Delivery rows still carry a stale Feed record.
        if any(not self._is_legacy_delivery(delivery) for delivery in deliveries):
            logger.info("Skipping MATERIAL_FEED for policy Delivery step_id=%s", job_step_id)
            return False
        for delivery in deliveries:
            if delivery.status == MaterialDeliveryStatus.COMPLETED:
                feed = feed_service.get_for_delivery(delivery.job_delivery_id)
                if feed and feed.status == MaterialFeedStatus.PENDING:
                    logger.info("Dispatching Material Feed %s", feed.feed_execution_id)
                    feed_coord = self._material_feed_execution_coordinator_factory(session)
                    req_id = str(uuid.uuid4())
                    try:
                        result = feed_coord.execute_feed(
                            feed_execution_id=feed.feed_execution_id,
                            req_id=req_id,
                            # A Material Feed is still a real Robot Cell Goal.
                            # Never let a legacy Feed path bypass the shared
                            # non-empty parts_json physical-command invariant.
                            parts_json=RobotCellActionAdapter.serialize_parts_payload(
                                ProductionCellPayloadBuilder(session).build_for_job_step(
                                    job_id=delivery.production_job_id,
                                    job_step_id=job_step_id,
                                ).parts
                            ),
                        )

                        if False:
                            pass

                        session.commit()
                        return True
                    except Exception:
                        logger.exception("Failed to dispatch material feed action")
                        session.rollback()
        return False
