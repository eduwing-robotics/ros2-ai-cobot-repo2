"""Generic PRE_ROOF inspection and execution-gate materialization lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from shared.datetime_utils import as_utc
from sqlalchemy.orm import Session

from shared.models.factory import (
    AssemblyRecipeStage,
    EventType,
    JobStatus,
    JobStep,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionResultCode,
    ProductionInspectionResult,
    ProductionInspectionStatus,
    ProductionInspectionType,
    ProductionInspectionViewRequest,
    ProductionJob,
)
from shared.services.material_delivery_service import MaterialDeliveryConfigurationError, MaterialDeliveryService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.schemas.pre_roof_vision import (
    PRE_ROOF_VIEW_ORDER,
    PreRoofInspectionRequestV01,
    PreRoofInspectionResultV01,
    PreRoofViewInspectionRequestV02,
    PreRoofViewInspectionResultV02,
    PreRoofViewResultCode,
)
from shared.realtime.production_inspection_events import get_production_inspection_change_callback
from shared.realtime.production_events import get_production_change_callback


PRE_ROOF_PASS_GATE = "PRE_ROOF_PASS"


class PreRoofVisionApplyDisposition(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    UNCORRELATED = "UNCORRELATED"


class ProductionCompletionError(RuntimeError):
    pass


class ProductionCompletionNotFoundError(ProductionCompletionError):
    pass


class InvalidProductionCompletionTransitionError(ProductionCompletionError):
    pass


# Compatibility names for callers that imported the old boundary errors. Their
# semantics are now generic gated-stage configuration failures.
class MissingRoofOptionError(ProductionCompletionError):
    pass


class RoofStageMaterializationError(ProductionCompletionError):
    pass


class ProductionCompletionService:
    """Transition a PRE_ROOF hold through a generic execution-gate release."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._changed_production_job_id: int | None = None
        self._changed_inspection_id: int | None = None

    def start_pre_roof_inspection(self, *, production_job_id: int) -> ProductionInspection:
        def operation() -> ProductionInspection:
            job = self._job_for_update(production_job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.PRE_ROOF_READY.value)
            self._require(bool(self._selected_gated_stages(job)), "execution gate", "MISSING", PRE_ROOF_PASS_GATE)
            inspection = self._latest_pre_roof_inspection_for_update(job.job_id)
            if inspection is None:
                inspection = self._new_pre_roof_cycle(job_id=job.job_id, cycle=1)
            elif (
                inspection.status is ProductionInspectionStatus.ERROR
                or (
                    inspection.status is ProductionInspectionStatus.COMPLETED
                    and (
                        inspection.result in {
                            ProductionInspectionResultCode.FAIL,
                            ProductionInspectionResultCode.NOT_EVALUATED,
                        }
                        or (
                            inspection.result is ProductionInspectionResultCode.PASS
                            and inspection.production_valid is False
                            and inspection.wire_request_snapshot_json is None
                        )
                    )
                )
            ):
                inspection = self._new_pre_roof_cycle(job_id=job.job_id, cycle=inspection.inspection_cycle + 1)
            self._require(inspection.status is ProductionInspectionStatus.PENDING, "production inspection", inspection.status.value, ProductionInspectionStatus.RUNNING.value)
            inspection.status = ProductionInspectionStatus.RUNNING
            inspection.started_at = self._utcnow()
            # v0.2 begins durably at TOP; reconciliation can recover this row
            # after a process restart without replaying passed views.
            self._active_or_next_view_request_locked(inspection)
            self._mark_inspection_changed(inspection)
            self._record_event(job.job_id, EventType.INSPECTION_STARTED, f"PRE_ROOF inspection cycle {inspection.inspection_cycle} started.")
            return inspection
        return self._write(operation)

    def pass_pre_roof_inspection(self, *, production_job_id: int) -> JobStep:
        """Trusted internal Server PASS seam; never call from Vision UDP result handling.

        A persisted wire request marks an inspection as Vision-managed. Such an
        inspection must be completed only by ``apply_pre_roof_vision_result``;
        this keeps the internal Fake/test seam from becoming a quality-authority
        bypass when it is used by application code.
        """
        def operation() -> JobStep:
            job = self._job_for_update(production_job_id)
            inspection = self._require_latest_pre_roof_inspection_for_update(job.job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.ROOF_READY.value)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.COMPLETED.value)
            has_v02_wire_request = self._session.scalar(select(ProductionInspectionViewRequest.view_request_id).where(
                ProductionInspectionViewRequest.inspection_id == inspection.inspection_id,
                ProductionInspectionViewRequest.status.in_(("SENT", "ACKED")),
            ).limit(1)) is not None
            if inspection.wire_request_snapshot_json is not None or has_v02_wire_request:
                raise InvalidProductionCompletionTransitionError(
                    "A wire-managed PRE_ROOF inspection may only be completed by validated Vision view results."
                )
            inspection.status = ProductionInspectionStatus.COMPLETED
            inspection.result = ProductionInspectionResultCode.PASS
            inspection.vision_production_valid = False
            inspection.production_valid = True
            inspection.is_passed = True
            inspection.failure_reason = None
            inspection.completed_at = self._utcnow()
            self._mark_inspection_changed(inspection)
            return self._materialize_gated_stages_locked(job, inspection, event_message="PRE_ROOF inspection passed; gated JobSteps are pending.")
        return self._write(operation)

    def prepare_pre_roof_wire_request(self, *, inspection_id: int) -> PreRoofInspectionRequestV01:
        """Deprecated v0.1 bundled request boundary; intentionally unsupported."""
        raise InvalidProductionCompletionTransitionError("PRE_ROOF v0.1 bundled requests are unsupported; use v0.2 per-view requests.")
        def operation() -> PreRoofInspectionRequestV01:
            inspection = self._session.scalar(select(ProductionInspection).where(ProductionInspection.inspection_id == inspection_id).with_for_update())
            if inspection is None or inspection.inspection_type is not ProductionInspectionType.PRE_ROOF:
                raise ProductionCompletionNotFoundError(f"PRE_ROOF inspection not found: inspection_id={inspection_id}.")
            job = self._job_for_update(inspection.production_job_id)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.RUNNING.value)
            if inspection.wire_request_snapshot_json:
                return PreRoofInspectionRequestV01.model_validate_json(inspection.wire_request_snapshot_json)
            request_timestamp = inspection.started_at or inspection.requested_at
            # PostgreSQL may return the same instant in the session timezone.
            # Keep the persisted source/instant; only the wire representation is UTC.
            request_timestamp = as_utc(request_timestamp)
            request = PreRoofInspectionRequestV01(
                inspection_request_id=inspection.inspection_request_id,
                inspection_cycle=inspection.inspection_cycle,
                job_id=job.job_id, job_code=job.job_code,
                timestamp=request_timestamp,
            )
            from hashlib import sha256
            import json
            snapshot = request.model_dump_json()
            inspection.wire_request_snapshot_json = snapshot
            inspection.wire_request_digest = sha256(snapshot.encode("utf-8")).hexdigest()
            return request
        return self._write(operation)

    def apply_pre_roof_vision_result(
        self, *, vision_result: PreRoofInspectionResultV01, result_digest: str
    ) -> PreRoofVisionApplyDisposition:
        """Deprecated v0.1 bundled final-result boundary; intentionally unsupported."""
        raise InvalidProductionCompletionTransitionError("PRE_ROOF v0.1 bundled results are unsupported; use v0.2 per-view results.")
        def operation() -> PreRoofVisionApplyDisposition:
            inspection = self._session.scalar(select(ProductionInspection).where(
                ProductionInspection.inspection_request_id == str(vision_result.inspection_request_id)
            ).with_for_update())
            if inspection is None or inspection.inspection_type is not ProductionInspectionType.PRE_ROOF:
                return PreRoofVisionApplyDisposition.UNCORRELATED
            if inspection.inspection_cycle != vision_result.inspection_cycle or inspection.production_job_id != vision_result.job_id:
                return PreRoofVisionApplyDisposition.UNCORRELATED
            latest = self._latest_pre_roof_inspection_for_update(inspection.production_job_id)
            if inspection.status is ProductionInspectionStatus.COMPLETED:
                return (PreRoofVisionApplyDisposition.DUPLICATE
                        if inspection.wire_result_digest == result_digest
                        else PreRoofVisionApplyDisposition.CONFLICT)
            # An ERROR cycle has no terminal canonical Result to compare. A
            # first result arriving after that cycle was abandoned is stale,
            # never a reason to mutate the newer cycle or revive the old one.
            if inspection.status is ProductionInspectionStatus.ERROR:
                return PreRoofVisionApplyDisposition.STALE
            if latest is None or latest.inspection_id != inspection.inspection_id:
                return PreRoofVisionApplyDisposition.STALE
            job = self._job_for_update(inspection.production_job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.PRE_ROOF_READY.value)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.COMPLETED.value)
            result_code = ProductionInspectionResultCode(vision_result.overall_result.value)
            inspection.status = ProductionInspectionStatus.COMPLETED
            inspection.result = result_code
            inspection.vision_production_valid = vision_result.vision_production_valid
            inspection.production_valid = result_code is ProductionInspectionResultCode.PASS and vision_result.vision_production_valid
            inspection.is_passed = True if result_code is ProductionInspectionResultCode.PASS else False if result_code is ProductionInspectionResultCode.FAIL else None
            inspection.failure_reason = None
            inspection.completed_at = self._utcnow()
            inspection.wire_result_digest = result_digest
            inspection.runtime_profile = vision_result.runtime_profile
            import json
            inspection.runtime_versions_json = json.dumps(vision_result.runtime_versions, sort_keys=True, separators=(",", ":"))
            view_codes = {"TOP": "VIEW_TOP", "LEFT": "VIEW_LEFT", "RIGHT": "VIEW_RIGHT", "FRONT": "VIEW_FRONT", "BEHIND": "VIEW_BEHIND"}
            for view in vision_result.views:
                code = ProductionInspectionResultCode(view.result.value)
                self._session.add(ProductionInspectionResult(
                    inspection_id=inspection.inspection_id, item_code=view_codes[view.view_name],
                    item_name=view.view_name, result=code,
                    is_passed=True if code is ProductionInspectionResultCode.PASS else False if code is ProductionInspectionResultCode.FAIL else None,
                    notes=None if view.reason_code is None else view.reason_code.value,
                ))
            self._mark_inspection_changed(inspection)
            if self.is_pre_roof_gate_open(inspection):
                self._materialize_gated_stages_locked(job, inspection, event_message="PRE_ROOF Vision result passed; gated JobSteps are pending.")
            else:
                event = EventType.INSPECTION_PASSED if result_code is ProductionInspectionResultCode.PASS else EventType.INSPECTION_FAILED
                self._record_event(job.job_id, event, f"PRE_ROOF Vision result: {result_code.value}.")
            return PreRoofVisionApplyDisposition.APPLIED
        return self._write(operation)

    def prepare_pre_roof_view_wire_request(self, *, inspection_id: int) -> tuple[int, PreRoofViewInspectionRequestV02]:
        """Create/recover exactly one durable v0.2 request for the next view.

        The failed-view re-request policy lives here: it keeps the same parent
        inspection cycle and assigns a fresh request ID only for that view.
        """
        def operation() -> tuple[int, PreRoofViewInspectionRequestV02]:
            inspection = self._session.scalar(select(ProductionInspection).where(
                ProductionInspection.inspection_id == inspection_id
            ).with_for_update())
            if inspection is None or inspection.inspection_type is not ProductionInspectionType.PRE_ROOF:
                raise ProductionCompletionNotFoundError(f"PRE_ROOF inspection not found: inspection_id={inspection_id}.")
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.RUNNING.value)
            job = self._job_for_update(inspection.production_job_id)
            request_row = self._active_or_next_view_request_locked(inspection)
            if request_row.request_snapshot_json is None:
                request = PreRoofViewInspectionRequestV02(
                    inspection_request_id=request_row.inspection_request_id,
                    inspection_cycle=inspection.inspection_cycle,
                    job_id=job.job_id,
                    job_code=job.job_code,
                    view_name=request_row.view_name,
                )
                from hashlib import sha256
                snapshot = request.model_dump_json()
                request_row.request_snapshot_json = snapshot
                request_row.request_digest = sha256(snapshot.encode("utf-8")).hexdigest()
            else:
                request = PreRoofViewInspectionRequestV02.model_validate_json(request_row.request_snapshot_json)
            self._mark_inspection_changed(inspection)
            return request_row.view_request_id, request
        return self._write(operation)

    def apply_pre_roof_view_ack(self, *, inspection_request_id: str, inspection_cycle: int, view_name: str, accepted: bool, duplicate: bool, reason_code: str | None) -> bool:
        """Persist only a positively correlated accepted/duplicate ACK."""
        def operation() -> bool:
            row = self._session.scalar(select(ProductionInspectionViewRequest).where(
                ProductionInspectionViewRequest.inspection_request_id == inspection_request_id
            ).with_for_update())
            if row is None or row.view_name != view_name:
                return False
            inspection = self._session.scalar(select(ProductionInspection).where(
                ProductionInspection.inspection_id == row.inspection_id
            ).with_for_update())
            if inspection is None or inspection.inspection_cycle != inspection_cycle:
                return False
            if not accepted:
                # A rejected datagram is never transport success.  Keep the
                # request durable but fail closed at the current inspection.
                if row.status not in {"COMPLETED", "FAILED"}:
                    row.status = "FAILED"
                    row.error_code = reason_code or "ACK_REJECTED"
                    inspection.status = ProductionInspectionStatus.ERROR
                    inspection.failure_reason = row.error_code
                    inspection.completed_at = self._utcnow()
                    self._mark_inspection_changed(inspection)
                return False
            if row.status == "COMPLETED":
                return True
            if row.status not in {"REQUESTED", "SENT", "ACKED"}:
                return False
            row.status = "ACKED"
            if row.acked_at is None:
                row.acked_at = self._utcnow()
            self._mark_inspection_changed(inspection)
            return True
        return self._write(operation)

    def apply_pre_roof_view_result(self, *, vision_result: PreRoofViewInspectionResultV02, result_digest: str) -> PreRoofVisionApplyDisposition:
        """Apply one correlated v0.2 view result and let the server choose next."""
        def operation() -> PreRoofVisionApplyDisposition:
            row = self._session.scalar(select(ProductionInspectionViewRequest).where(
                ProductionInspectionViewRequest.inspection_request_id == str(vision_result.inspection_request_id)
            ).with_for_update())
            if row is None:
                return PreRoofVisionApplyDisposition.UNCORRELATED
            inspection = self._session.scalar(select(ProductionInspection).where(
                ProductionInspection.inspection_id == row.inspection_id
            ).with_for_update())
            if inspection is None or inspection.inspection_type is not ProductionInspectionType.PRE_ROOF:
                return PreRoofVisionApplyDisposition.UNCORRELATED
            if (inspection.inspection_cycle != vision_result.inspection_cycle or inspection.production_job_id != vision_result.job_id or row.view_name != vision_result.view_name.value):
                return PreRoofVisionApplyDisposition.UNCORRELATED
            if row.status == "COMPLETED":
                return PreRoofVisionApplyDisposition.DUPLICATE if row.result_digest == result_digest else PreRoofVisionApplyDisposition.CONFLICT
            latest = self._latest_pre_roof_inspection_for_update(inspection.production_job_id)
            if latest is None or latest.inspection_id != inspection.inspection_id or inspection.status is not ProductionInspectionStatus.RUNNING:
                return PreRoofVisionApplyDisposition.STALE
            expected = self._next_required_view_locked(inspection)
            if expected != row.view_name or row.status not in {"REQUESTED", "SENT", "ACKED"}:
                return PreRoofVisionApplyDisposition.STALE
            job = self._job_for_update(inspection.production_job_id)
            code = ProductionInspectionResultCode(vision_result.result.value)
            row.status = "COMPLETED"
            row.result = code
            row.result_digest = result_digest
            row.runtime_version = vision_result.runtime_version
            row.runtime_port = vision_result.runtime_port
            row.vision_production_valid = vision_result.production_valid
            row.completed_at = self._utcnow()
            self._session.add(ProductionInspectionResult(
                inspection_id=inspection.inspection_id,
                item_code=f"VIEW_{row.view_name}", item_name=row.view_name,
                result=code, is_passed=(code is ProductionInspectionResultCode.PASS),
                notes=None,
            ))
            # FMS sessions intentionally run with autoflush disabled.  Persist
            # the just-completed view before deriving the server-owned next
            # view, otherwise TOP still looks active to the SQL selectors.
            self._session.flush()
            if code is ProductionInspectionResultCode.FAIL:
                self._session.add(ProductionInspectionViewRequest(
                    inspection_id=inspection.inspection_id, view_name=row.view_name, status="REQUESTED"
                ))
                self._record_event(job.job_id, EventType.INSPECTION_FAILED, f"PRE_ROOF {row.view_name} view failed; reinspection requested.")
                self._mark_inspection_changed(inspection)
                return PreRoofVisionApplyDisposition.APPLIED
            next_view = self._next_required_view_locked(inspection)
            if next_view is None:
                inspection.status = ProductionInspectionStatus.COMPLETED
                inspection.result = ProductionInspectionResultCode.PASS
                # Vision runtime authorization is provenance only in v0.2.
                inspection.vision_production_valid = False
                inspection.production_valid = False
                inspection.is_passed = True
                inspection.failure_reason = None
                inspection.completed_at = self._utcnow()
                self._mark_inspection_changed(inspection)
                self._materialize_gated_stages_locked(job, inspection, event_message="PRE_ROOF five-view inspection passed; gated JobSteps are pending.")
            else:
                self._active_or_next_view_request_locked(inspection)
                self._mark_inspection_changed(inspection)
            return PreRoofVisionApplyDisposition.APPLIED
        return self._write(operation)

    def _next_required_view_locked(self, inspection: ProductionInspection) -> str | None:
        passed = set(self._session.scalars(select(ProductionInspectionViewRequest.view_name).where(
            ProductionInspectionViewRequest.inspection_id == inspection.inspection_id,
            ProductionInspectionViewRequest.status == "COMPLETED",
            ProductionInspectionViewRequest.result == ProductionInspectionResultCode.PASS,
        )))
        return next((view for view in PRE_ROOF_VIEW_ORDER if view not in passed), None)

    def _active_or_next_view_request_locked(self, inspection: ProductionInspection) -> ProductionInspectionViewRequest:
        active = self._session.scalar(select(ProductionInspectionViewRequest).where(
            ProductionInspectionViewRequest.inspection_id == inspection.inspection_id,
            ProductionInspectionViewRequest.status.in_(("REQUESTED", "SENT", "ACKED")),
        ).order_by(ProductionInspectionViewRequest.view_request_id.desc()).limit(1).with_for_update())
        if active is not None:
            return active
        view_name = self._next_required_view_locked(inspection)
        if view_name is None:
            raise InvalidProductionCompletionTransitionError("PRE_ROOF inspection already has all view PASS results.")
        active = ProductionInspectionViewRequest(inspection_id=inspection.inspection_id, view_name=view_name, status="REQUESTED")
        self._session.add(active)
        self._session.flush()
        return active

    def mark_pre_roof_wire_error(
        self, *, inspection_request_id: str, inspection_cycle: int, reason_code: str
    ) -> bool:
        """Fail closed only a correlated, current, running PRE_ROOF inspection."""
        def operation() -> bool:
            inspection = self._session.scalar(select(ProductionInspection).where(
                ProductionInspection.inspection_request_id == inspection_request_id
            ).with_for_update())
            if inspection is None or inspection.inspection_type is not ProductionInspectionType.PRE_ROOF or inspection.inspection_cycle != inspection_cycle:
                return False
            latest = self._latest_pre_roof_inspection_for_update(inspection.production_job_id)
            if latest is None or latest.inspection_id != inspection.inspection_id or inspection.status is not ProductionInspectionStatus.RUNNING:
                return False
            job = self._job_for_update(inspection.production_job_id)
            inspection.status = ProductionInspectionStatus.ERROR
            inspection.result = None
            inspection.vision_production_valid = False
            inspection.production_valid = False
            inspection.is_passed = None
            inspection.failure_reason = reason_code
            inspection.wire_error_code = reason_code
            inspection.completed_at = self._utcnow()
            self._mark_inspection_changed(inspection)
            self._record_event(job.job_id, EventType.INSPECTION_FAILED, f"PRE_ROOF wire error: {reason_code}")
            return True
        return self._write(operation)

    def _materialize_gated_stages_locked(self, job: ProductionJob, inspection: ProductionInspection, *, event_message: str) -> JobStep:
        self._require(self.is_pre_roof_gate_open(inspection), "PRE_ROOF gate", "CLOSED", "OPEN")
        stages = self._selected_gated_stages(job)
        if not stages:
            raise RoofStageMaterializationError("PRE_ROOF PASS requires at least one selected gated RecipeStage.")
        materialized: list[JobStep] = []
        delivery_service = MaterialDeliveryService(self._session)
        for stage in stages:
            existing = self._session.scalar(select(JobStep).where(JobStep.job_id == job.job_id, JobStep.source_recipe_stage_id == stage.recipe_stage_id).with_for_update())
            if existing is not None:
                raise InvalidProductionCompletionTransitionError("Gated RecipeStage is already materialized for this production job.")
            step = ProductionOrchestrationService.snapshot_recipe_stage_to_job_step(job_id=job.job_id, stage=stage)
            self._session.add(step)
            self._session.flush()
            try:
                delivery_service.bind_pre_materialized_gated_stage_item(job=job, stage=stage, job_step=step)
            except MaterialDeliveryConfigurationError as exc:
                raise RoofStageMaterializationError(str(exc)) from exc
            materialized.append(step)
        job.status = JobStatus.ROOF_READY
        self._mark_production_changed(job.job_id)
        self._record_event(job.job_id, EventType.INSPECTION_PASSED, event_message)
        self._session.flush()
        return materialized[0]

    def fail_pre_roof_inspection(self, *, production_job_id: int, reason: str | None = None) -> ProductionInspection:
        normalized_reason = self._reason(reason or "Operator reported PRE_ROOF inspection failure.")
        def operation() -> ProductionInspection:
            job = self._job_for_update(production_job_id)
            inspection = self._require_latest_pre_roof_inspection_for_update(job.job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.PRE_ROOF_READY.value)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.COMPLETED.value)
            inspection.status = ProductionInspectionStatus.COMPLETED
            inspection.result = ProductionInspectionResultCode.FAIL
            inspection.production_valid = False
            inspection.is_passed = False
            inspection.completed_at = self._utcnow()
            inspection.failure_reason = normalized_reason
            self._mark_inspection_changed(inspection)
            self._record_event(job.job_id, EventType.INSPECTION_FAILED, f"PRE_ROOF inspection failed: {normalized_reason}")
            return inspection
        return self._write(operation)

    def not_evaluated_pre_roof_inspection(self, *, production_job_id: int, reason: str | None = None) -> ProductionInspection:
        """Terminal non-quality result; holds roof materialization and permits a new cycle."""
        normalized_reason = self._reason(reason or "PRE_ROOF inspection could not be evaluated.")
        def operation() -> ProductionInspection:
            job = self._job_for_update(production_job_id)
            inspection = self._require_latest_pre_roof_inspection_for_update(job.job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.PRE_ROOF_READY.value)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.COMPLETED.value)
            inspection.status = ProductionInspectionStatus.COMPLETED
            inspection.result = ProductionInspectionResultCode.NOT_EVALUATED
            inspection.production_valid = False
            inspection.is_passed = None
            inspection.completed_at = self._utcnow()
            inspection.failure_reason = normalized_reason
            self._mark_inspection_changed(inspection)
            self._record_event(job.job_id, EventType.INSPECTION_FAILED, f"PRE_ROOF inspection not evaluated: {normalized_reason}")
            return inspection
        return self._write(operation)

    def error_pre_roof_inspection(self, *, production_job_id: int, reason: str | None = None) -> ProductionInspection:
        """Record transport/runtime failure without misclassifying it as quality FAIL."""
        normalized_reason = self._reason(reason or "PRE_ROOF inspection runtime error.")
        def operation() -> ProductionInspection:
            job = self._job_for_update(production_job_id)
            inspection = self._require_latest_pre_roof_inspection_for_update(job.job_id)
            self._require(job.status is JobStatus.PRE_ROOF_READY, "production job", job.status.value, JobStatus.PRE_ROOF_READY.value)
            self._require(inspection.status is ProductionInspectionStatus.RUNNING, "production inspection", inspection.status.value, ProductionInspectionStatus.ERROR.value)
            inspection.status = ProductionInspectionStatus.ERROR
            inspection.result = None
            inspection.production_valid = False
            inspection.is_passed = None
            inspection.completed_at = self._utcnow()
            inspection.failure_reason = normalized_reason
            self._mark_inspection_changed(inspection)
            self._record_event(job.job_id, EventType.INSPECTION_FAILED, f"PRE_ROOF inspection error: {normalized_reason}")
            return inspection
        return self._write(operation)

    @staticmethod
    def is_pre_roof_gate_open(inspection: ProductionInspection) -> bool:
        """Server-owned roof gate; Vision readiness alone is intentionally insufficient."""
        return (
            inspection.status is ProductionInspectionStatus.COMPLETED
            and inspection.result is ProductionInspectionResultCode.PASS
        )

    def _selected_gated_stages(self, job: ProductionJob) -> list[AssemblyRecipeStage]:
        if job.assembly_recipe_id is None:
            return []
        stages = list(self._session.scalars(
            select(AssemblyRecipeStage).where(
                AssemblyRecipeStage.recipe_id == job.assembly_recipe_id,
                AssemblyRecipeStage.execution_gate == PRE_ROOF_PASS_GATE,
            ).order_by(AssemblyRecipeStage.stage_order).with_for_update()
        ))
        return [
            stage for stage in stages
            if stage.option_code is None
            or (job.roof_option_code is not None and stage.option_code == job.roof_option_code.value)
        ]

    def _job_for_update(self, job_id: int) -> ProductionJob:
        job = self._session.scalar(select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update())
        if job is None:
            raise ProductionCompletionNotFoundError(f"Production job not found: job_id={job_id}.")
        return job

    def _latest_pre_roof_inspection_for_update(self, job_id: int) -> ProductionInspection | None:
        return self._session.scalar(select(ProductionInspection).where(
            ProductionInspection.production_job_id == job_id,
            ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
        ).order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc()).limit(1).with_for_update())

    def _require_latest_pre_roof_inspection_for_update(self, job_id: int) -> ProductionInspection:
        inspection = self._latest_pre_roof_inspection_for_update(job_id)
        if inspection is None:
            raise ProductionCompletionNotFoundError(f"PRE_ROOF inspection not found for production_job_id={job_id}.")
        return inspection

    def _new_pre_roof_cycle(self, *, job_id: int, cycle: int) -> ProductionInspection:
        inspection = ProductionInspection(
            production_job_id=job_id,
            inspection_type=ProductionInspectionType.PRE_ROOF,
            inspection_cycle=cycle,
            status=ProductionInspectionStatus.PENDING,
            result=None,
            production_valid=False,
            vision_production_valid=False,
            is_passed=None,
        )
        self._session.add(inspection)
        self._session.flush()
        return inspection

    def _record_event(self, job_id: int, event_type: EventType, message: str) -> None:
        self._session.add(ProductionEvent(job_id=job_id, job_step_id=None, event_type=event_type, message=message))

    def _mark_inspection_changed(self, inspection: ProductionInspection) -> None:
        self._changed_inspection_id = inspection.inspection_id

    def _mark_production_changed(self, job_id: int) -> None:
        self._changed_production_job_id = job_id

    def _write(self, operation):
        self._changed_inspection_id = None
        self._changed_production_job_id = None
        try:
            result = operation()
            self._session.commit()
            inspection_id = self._changed_inspection_id
            production_job_id = self._changed_production_job_id
            self._changed_inspection_id = None
            self._changed_production_job_id = None
            if inspection_id is not None:
                callback = get_production_inspection_change_callback()
                if callback is not None:
                    try:
                        callback(inspection_id)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception("ProductionInspection post-commit notification failed id=%s", inspection_id)
            if production_job_id is not None:
                callback = get_production_change_callback()
                if callback is not None:
                    try:
                        callback(production_job_id, "pre_roof_pass_roof_materialized")
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception("Production post-commit notification failed job_id=%s", production_job_id)
            return result
        except Exception:
            self._changed_inspection_id = None
            self._changed_production_job_id = None
            self._session.rollback()
            raise


    @staticmethod
    def _require(valid: bool, entity: str, current: str, target: str) -> None:
        if not valid:
            raise InvalidProductionCompletionTransitionError(f"Invalid {entity} lifecycle transition: {current} -> {target}.")

    @staticmethod
    def _reason(value: str) -> str:
        if not isinstance(value, str) or not (normalized := value.strip()):
            raise ProductionCompletionError("failure reason must be a non-empty string.")
        return normalized

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
