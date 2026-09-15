"""Transport-neutral coordinator for durable MATERIAL_FEED runtime execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fms_server.cell_action_transport import CellTaskExecutionResult, CellTaskFeedbackCallback
from fms_server.cell_status import CellStatusStore
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, RobotCellActionAdapterError
from shared.models.factory import JobMaterialFeedExecution, MaterialDeliveryStatus, MaterialFeedStatus, ProductionJob
from shared.services.material_feed_execution_service import MaterialFeedExecutionService


class MaterialFeedCoordinatorOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    CELL_FAILED = "CELL_FAILED"
    GOAL_REJECTED = "GOAL_REJECTED"
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    UNCERTAIN_RESULT_TIMEOUT = "UNCERTAIN_RESULT_TIMEOUT"
    UNCERTAIN_TRANSPORT_ERROR = "UNCERTAIN_TRANSPORT_ERROR"
    CELL_CANCELED = "CELL_CANCELED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    CONTRACT_BLOCKED = "CONTRACT_BLOCKED"
    CELL_UNAVAILABLE = "CELL_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class MaterialFeedCoordinatorResult:
    outcome: MaterialFeedCoordinatorOutcome
    feed_execution_id: int
    req_id: str
    stable_step_id: str
    cell_result: CellTaskExecutionResult | None = None
    started: bool = False
    requires_reconciliation: bool = False
    detail: str | None = None


class MaterialFeedExecutionCoordinator:
    """Dispatch a single eligible Feed without mutating Assembly JobSteps or Job."""

    def __init__(self, session: Session, *, robot_cell_adapter: RobotCellActionAdapter, cell_status_store: CellStatusStore | None = None) -> None:
        self._session = session
        self._adapter = robot_cell_adapter
        self._feeds = MaterialFeedExecutionService(session)
        self._cell_status_store = cell_status_store

    @staticmethod
    def stable_step_id(feed_execution_id: int) -> str:
        return f"MATERIAL-FEED-{feed_execution_id}"

    def execute_feed(
        self,
        *,
        feed_execution_id: int,
        req_id: str,
        parts_json: str,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> MaterialFeedCoordinatorResult:
        feed, job = self._context(feed_execution_id)
        stable_step_id = self.stable_step_id(feed.feed_execution_id)
        if feed.status is MaterialFeedStatus.RUNNING:
            return self._result(MaterialFeedCoordinatorOutcome.ALREADY_RUNNING, feed, req_id, requires_reconciliation=True)
        if feed.status in {MaterialFeedStatus.COMPLETED, MaterialFeedStatus.FAILED}:
            return self._result(MaterialFeedCoordinatorOutcome.ALREADY_TERMINAL, feed, req_id)
        if feed.status is not MaterialFeedStatus.PENDING or feed.job_delivery.status is not MaterialDeliveryStatus.COMPLETED:
            return self._result(MaterialFeedCoordinatorOutcome.NOT_ELIGIBLE, feed, req_id)
        if self._cell_status_store is not None:
            availability = self._cell_status_store.dispatch_availability()
            if not availability.dispatch_allowed:
                return self._result(MaterialFeedCoordinatorOutcome.CELL_UNAVAILABLE, feed, req_id, detail=availability.reason)
        started = False
        def accepted() -> None:
            nonlocal started
            self._feeds.start_feed(feed.feed_execution_id)
            started = True
        try:
            command = self._adapter.build_task_command(
                job=job, task_type="MATERIAL_FEED", opaque_step_id=stable_step_id,
                req_id=req_id, parts_json=parts_json,
            )
            cell_result = self._adapter.execute(
                command, goal_accepted_callback=accepted, feedback_callback=feedback_callback
            )
        except RobotCellActionAdapterError as exc:
            return self._result(MaterialFeedCoordinatorOutcome.CONTRACT_BLOCKED, feed, req_id, detail=str(exc))
        if cell_result.succeeded is True:
            if not started:
                return self._result(MaterialFeedCoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR, feed, req_id, cell_result=cell_result, requires_reconciliation=True)
            self._feeds.complete_feed(feed.feed_execution_id, completed_slots=cell_result.completed_slots)
            return self._result(MaterialFeedCoordinatorOutcome.COMPLETED, feed, req_id, cell_result=cell_result, started=True)
        if cell_result.succeeded is False:
            if not started:
                return self._result(MaterialFeedCoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR, feed, req_id, cell_result=cell_result, requires_reconciliation=True)
            self._feeds.fail_feed(feed.feed_execution_id, error_code=cell_result.error_code, failure_reason=cell_result.detail, completed_slots=cell_result.completed_slots)
            return self._result(MaterialFeedCoordinatorOutcome.CELL_FAILED, feed, req_id, cell_result=cell_result, started=True)
        names = {"GOAL_REJECTED": MaterialFeedCoordinatorOutcome.GOAL_REJECTED, "SERVER_UNAVAILABLE": MaterialFeedCoordinatorOutcome.SERVER_UNAVAILABLE, "RESULT_TIMEOUT": MaterialFeedCoordinatorOutcome.UNCERTAIN_RESULT_TIMEOUT, "CELL_CANCELED": MaterialFeedCoordinatorOutcome.CELL_CANCELED}
        outcome = names.get(cell_result.outcome.value, MaterialFeedCoordinatorOutcome.UNCERTAIN_TRANSPORT_ERROR)
        return self._result(outcome, feed, req_id, cell_result=cell_result, started=started, requires_reconciliation=started)

    def _context(self, feed_execution_id: int) -> tuple[JobMaterialFeedExecution, ProductionJob]:
        feed = self._session.scalar(select(JobMaterialFeedExecution).options(joinedload(JobMaterialFeedExecution.job_delivery)).where(JobMaterialFeedExecution.feed_execution_id == feed_execution_id))
        if feed is None:
            raise ValueError(f"Unknown feed_execution_id={feed_execution_id}.")
        job = self._session.scalar(select(ProductionJob).options(joinedload(ProductionJob.product)).where(ProductionJob.job_id == feed.job_delivery.production_job_id))
        if job is None:
            raise RuntimeError("Feed execution delivery has no ProductionJob.")
        return feed, job

    def _result(self, outcome, feed, req_id, *, cell_result=None, started=False, requires_reconciliation=False, detail=None):
        return MaterialFeedCoordinatorResult(outcome, feed.feed_execution_id, req_id, self.stable_step_id(feed.feed_execution_id), cell_result, started, requires_reconciliation, detail)
