"""Read-only production monitoring endpoints for GUI and development tools."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fms_server.empty_pallet_return_service import (
    EmptyPalletReturnDuplicateError,
    EmptyPalletReturnError,
)
from fms_server.forklift_action_adapter import FakeForkliftActionTransport, ForkliftActionAdapter
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.operator_empty_pallet_return_service import OperatorEmptyPalletReturnService
from fms_server.incoming_qa_v02_orchestration_service import (
    IncomingQAV02ActiveInspectionError,
    IncomingQAV02OrchestrationError,
    IncomingQAV02OrchestrationService,
)
from fms_server.incoming_material_qa_dispatch_coordinator import (
    IncomingMaterialQADispatchAction,
    IncomingMaterialQADispatchCoordinator,
)
from fms_server.incoming_material_qa_http_client import (
    IncomingMaterialQAHttpConflictError,
    IncomingMaterialQAHttpError,
    IncomingMaterialQAHttpTransportError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from api_server.routers.inventory import get_db
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionType,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.schemas.production import (
    ProductionExecutionSnapshotResponse,
    ProductionEventResponse,
    IncomingQAOperatorStartResponse,
    IncomingQAV02MonitorResponse,
    IncomingQAV02PlanRequest,
    IncomingQAV02PlanResponse,
    IncomingQATestHoldRequest,
    IncomingQATestHoldResponse,
    EmptyPalletReturnCommandRequest,
    EmptyPalletReturnCommandResponse,
    JobMaterialDeliveryResponse,
    ManualPrestageCommandResponse,
    ManualTransportRecoveryRequest,
    ManualTransportRecoveryResponse,
    PhysicalReadyCommandRequest,
    PhysicalReadyCommandResponse,
    PreRoofFailCommandRequest,
    ProductionInspectionResponse,
    ProductionJobDetailResponse,
    ProductionJobCancelResponse,
    ProductionJobStepResponse,
    OperatorExecutionReadyCommandResponse,
    OuterWallBatchOperatorReadyResponse,
    ProductionJobSummaryResponse,
)
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionJobNotFoundError,
    ProductionOrchestrationService,
)
from shared.services.production_completion_service import (
    InvalidProductionCompletionTransitionError,
    ProductionCompletionError,
    ProductionCompletionNotFoundError,
    ProductionCompletionService,
)
from shared.services.unity_current_stage_projection_service import UnityCurrentStageProjectionService
from shared.services.production_execution_snapshot_service import (
    ProductionExecutionSnapshotInconsistencyError,
    ProductionExecutionSnapshotJobNotFoundError,
    ProductionExecutionSnapshotService,
)
from shared.services.material_delivery_monitoring_service import MaterialDeliveryMonitoringService
from shared.services.transport_eligibility_service import TransportEligibilityService
from shared.services.incoming_qa_v02_monitoring_service import IncomingQAV02MonitoringService
from shared.vision_recipe_mapping import IncomingQAInspectionMode
from shared.services.unity_error_projection_service import UnityErrorProjectionService
from shared.services.manual_prestage_service import (
    ManualPrestageDeliveryNotFoundError,
    ManualPrestageError,
    ManualPrestageJobNotFoundError,
    ManualPrestagePolicyError,
    ManualPrestageService,
    ManualPrestageStateError,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.config import get_settings
from shared.models.factory import ExecutionAttempt, ExecutorType
from shared.services.operator_execution_ready_service import (
    OperatorExecutionReadyDeliveryIncompleteError,
    OperatorExecutionReadyJobNotFoundError,
    OperatorExecutionReadyPolicyError,
    OperatorExecutionReadyService,
    OperatorExecutionReadyStateError,
    OperatorExecutionReadyStepNotFoundError,
)
from shared.services.physical_ready_service import (
    PhysicalReadyError,
    PhysicalReadyPolicyError,
    PhysicalReadyService,
    PhysicalReadyStateError,
)

router = APIRouter(prefix="/production", tags=["Production"])


def get_incoming_material_qa_dispatch_coordinator(
    db: Annotated[Session, Depends(get_db)],
) -> IncomingMaterialQADispatchCoordinator:
    """Production command dependency; tests override this with a fake Vision runtime."""

    return IncomingMaterialQADispatchCoordinator(db)


def get_operator_empty_pallet_return_service(
    db: Annotated[Session, Depends(get_db)],
) -> OperatorEmptyPalletReturnService:
    """Temporary operator control is intentionally available only in fake FMS mode.

    The final TurtleBot Action runtime belongs to FMS. This API must not claim a
    real pallet moved through a fake adapter; tests may override this dependency.
    """
    if get_settings().cell_transport != "fake":
        raise HTTPException(
            status_code=503,
            detail="Empty-pallet return operator dispatch requires the configured FMS TurtleBot runtime.",
        )
    coordinator = ForkliftExecutionCoordinator(
        db,
        adapter=ForkliftActionAdapter(FakeForkliftActionTransport()),
        execution_attempt_service=ExecutionAttemptService(db),
    )
    return OperatorEmptyPalletReturnService(
        db,
        forklift_execution_coordinator=coordinator,
    )


def _job_not_found(job_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Production job not found: job_id={job_id}.")


def _require_delivery_incoming_qa_release(
    db: Session, delivery: JobMaterialDelivery
) -> None:
    """Reject downstream operator readiness until every persisted item is released."""

    if not TransportEligibilityService(db).are_all_delivery_items_released(delivery):
        raise HTTPException(
            status_code=409,
            detail="Incoming QA RELEASE is required before material preparation can be confirmed.",
        )


def _to_step_response(step: JobStep) -> ProductionJobStepResponse:
    return ProductionJobStepResponse(
        job_step_id=step.job_step_id,
        step_order=step.resolved_step_order,
        step_code=step.resolved_step_code,
        step_name=step.resolved_display_name,
        operation_code=step.operation_code,
        source_recipe_stage_id=step.source_recipe_stage_id,
        supply_mode=step.supply_mode,
        status=step.status,
        started_at=step.started_at,
        completed_at=step.completed_at,
        failure_reason=step.failure_reason,
        operator_execution_ready_at=step.operator_execution_ready_at,
    )


def _current_step(job_status: JobStatus, steps: list[JobStep]) -> JobStep | None:
    """Return an active/next Step only while the owning Job is RUNNING."""

    # READY is intentionally not treated as executable until its policy is agreed.
    if job_status not in {JobStatus.RUNNING, JobStatus.ROOF_READY}:
        return None

    ordered_steps = sorted(steps, key=lambda step: step.resolved_step_order)
    for status in (StepStatus.RUNNING, StepStatus.PENDING):
        current = next((step for step in ordered_steps if step.status is status), None)
        if current is not None:
            return current
    return None


@router.get("/jobs", response_model=list[ProductionJobSummaryResponse])
def get_production_jobs(
    db: Annotated[Session, Depends(get_db)],
) -> list[ProductionJob]:
    """Return Jobs ordered by most recently requested first."""

    statement = select(ProductionJob).options(selectinload(ProductionJob.assembly_recipe)).order_by(
        ProductionJob.requested_at.desc(),
        ProductionJob.job_id.desc(),
    )
    return list(db.scalars(statement))


@router.post("/jobs/{job_id}/cancel", response_model=ProductionJobCancelResponse)
def cancel_production_job(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionJob:
    """Cancel exactly the selected non-terminal Job through orchestration authority."""

    try:
        return ProductionOrchestrationService(db).cancel_job(
            job_id, reason="Monitoring GUI selected-job cancellation."
        )
    except ProductionJobNotFoundError as exc:
        raise _job_not_found(job_id) from exc
    except InvalidProductionStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=ProductionJobDetailResponse)
def get_production_job(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionJobDetailResponse:
    """Return one Job with all ordered Steps and its current executable/active Step."""

    statement = (
        select(ProductionJob)
        .options(selectinload(ProductionJob.steps), selectinload(ProductionJob.assembly_recipe))
        .where(ProductionJob.job_id == job_id)
    )
    job = db.scalar(statement)
    if job is None:
        raise _job_not_found(job_id)

    ordered_steps = sorted(job.steps, key=lambda step: step.resolved_step_order)
    current_step = _current_step(job.status, ordered_steps)
    process_stage = UnityCurrentStageProjectionService(db).derive_process_stage(job=job) or {}
    return ProductionJobDetailResponse(
        job_id=job.job_id,
        job_code=job.job_code,
        product_code=job.product_code,
        roof_option_code=job.roof_option_code,
        assembly_recipe_id=job.assembly_recipe_id,
        assembly_recipe_version=job.assembly_recipe.version if job.assembly_recipe is not None else None,
        source_pending_request_id=job.source_pending_request_id,
        source_item_index=job.source_item_index,
        status=job.status,
        control_state=job.control_state,
        requested_at=job.requested_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        process_stage_code=process_stage.get("process_stage_code"),
        process_stage_order=process_stage.get("process_stage_order"),
        process_stage_display_name=process_stage.get("process_stage_display_name"),
        steps=[_to_step_response(step) for step in ordered_steps],
        current_step=_to_step_response(current_step) if current_step is not None else None,
    )


@router.get("/jobs/{job_id}/material-deliveries", response_model=list[JobMaterialDeliveryResponse])
def get_job_material_deliveries(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> list[JobMaterialDeliveryResponse]:
    """Return a read-only Delivery -> Item -> latest QA monitoring drill-down."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    return MaterialDeliveryMonitoringService(db).get_deliveries_for_job(job_id=job_id)


@router.get("/jobs/{job_id}/incoming-qa/v02", response_model=IncomingQAV02MonitorResponse)
def get_incoming_qa_v02_monitor(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQAV02MonitorResponse:
    """Return persisted v0.2 request/item diagnostics; this endpoint never dispatches or mutates."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    return IncomingQAV02MonitoringService(db).get_job_monitor(job_id=job_id)


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/physical-ready",
    response_model=PhysicalReadyCommandResponse,
)
def confirm_material_delivery_physical_ready(
    job_id: int,
    job_delivery_id: int,
    command: PhysicalReadyCommandRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PhysicalReadyCommandResponse:
    """Persist physical preparation only; FMS dispatch remains asynchronous."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    delivery = db.get(JobMaterialDelivery, job_delivery_id)
    if delivery is None or delivery.production_job_id != job_id:
        raise HTTPException(status_code=404, detail="Job material delivery not found for production job.")
    if delivery.supply_mode is SupplyMode.TRANSPORTED:
        _require_delivery_incoming_qa_release(db, delivery)

    try:
        confirmed = PhysicalReadyService(db).confirm_physical_ready(
            job_delivery_id=job_delivery_id,
            request_id=command.request_id,
        )
    except (PhysicalReadyPolicyError, PhysicalReadyStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PhysicalReadyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    assert confirmed.supply_mode is not None
    assert confirmed.supply_group_code is not None
    assert confirmed.physical_ready_at is not None
    assert confirmed.physical_ready_request_id is not None
    return PhysicalReadyCommandResponse(
        job_id=job_id,
        job_delivery_id=confirmed.job_delivery_id,
        supply_mode=confirmed.supply_mode,
        supply_group_code=confirmed.supply_group_code,
        supply_destination_code=confirmed.supply_destination_code,
        physical_ready_at=confirmed.physical_ready_at,
        physical_ready_request_id=confirmed.physical_ready_request_id,
    )

@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/empty-pallet-return",
    response_model=EmptyPalletReturnCommandResponse,
)
def confirm_empty_pallet_and_return(
    job_id: int,
    job_delivery_id: int,
    command: EmptyPalletReturnCommandRequest,
    db: Annotated[Session, Depends(get_db)],
    service: Annotated[
        OperatorEmptyPalletReturnService,
        Depends(get_operator_empty_pallet_return_service),
    ],
) -> EmptyPalletReturnCommandResponse:
    """Explicit operator trigger; no direct DROP/Delivery mutation is permitted."""
    if not command.confirmed_empty:
        raise HTTPException(
            status_code=422,
            detail="confirmed_empty must be true after physical pallet verification.",
        )
    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    try:
        result = service.execute_confirmed_empty_return(
            production_job_id=job_id,
            job_delivery_id=job_delivery_id,
        )
    except EmptyPalletReturnDuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmptyPalletReturnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    attempt = db.scalar(
        select(ExecutionAttempt)
        .where(
            ExecutionAttempt.job_delivery_id == job_delivery_id,
            ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
            ExecutionAttempt.command_type == "EXECUTE_TRANSPORT_EMPTY_RETURN",
        )
        .order_by(ExecutionAttempt.attempt_id.desc())
    )
    if attempt is None:
        raise HTTPException(status_code=500, detail="Empty-pallet return Attempt was not persisted.")
    import json
    payload = json.loads(attempt.request_payload_json)
    return EmptyPalletReturnCommandResponse(
        job_id=job_id,
        job_delivery_id=job_delivery_id,
        attempt_id=attempt.attempt_id,
        request_id=attempt.req_id,
        pickup_code=payload["pickup_code"],
        dropoff_code=payload["dropoff_code"],
        status=result.status.value,
    )


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/transport-recovery",
    response_model=ManualTransportRecoveryResponse,
)
def confirm_transport_attempt_location(
    job_id: int,
    job_delivery_id: int,
    command: ManualTransportRecoveryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ManualTransportRecoveryResponse:
    """Internal operator recovery from an observed physical endpoint only.

    This endpoint never dispatches, retries, or creates a TurtleBot transport.
    """
    from fms_server.manual_transport_recovery_service import (
        ManualTransportRecoveryConflictError,
        ManualTransportRecoveryError,
        ManualTransportRecoveryNotFoundError,
        ManualTransportRecoveryService,
    )

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    try:
        result = ManualTransportRecoveryService(db).confirm_location(
            production_job_id=job_id,
            job_delivery_id=job_delivery_id,
            attempt_id=command.attempt_id,
            confirmed_location_code=command.confirmed_location_code,
            operator_note=command.operator_note,
        )
    except ManualTransportRecoveryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ManualTransportRecoveryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManualTransportRecoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ManualTransportRecoveryResponse(
        job_id=job_id,
        delivery_id=result.delivery_id,
        attempt_id=result.attempt_id,
        command_type=result.command_type,
        confirmed_location_code=result.confirmed_location_code,
        attempt_status=result.attempt_status,
        delivery_status=result.delivery_status,
        derived_drop_state=result.derived_drop_state.value,
        recovery_applied=result.recovery_applied,
    )


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/manual-prestage-ready",
    response_model=ManualPrestageCommandResponse,
)
def confirm_material_delivery_manual_prestage_ready(
    job_id: int,
    job_delivery_id: int,
    command: PhysicalReadyCommandRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ManualPrestageCommandResponse:
    """Persist MANUAL at-cell prestage evidence only; dispatch remains asynchronous."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    delivery = db.get(JobMaterialDelivery, job_delivery_id)
    if delivery is None or delivery.production_job_id != job_id:
        raise HTTPException(status_code=404, detail="Job material delivery not found for production job.")
    if delivery.supply_mode is SupplyMode.MANUAL:
        _require_delivery_incoming_qa_release(db, delivery)

    try:
        confirmed = ManualPrestageService(db).confirm_manual_prestage_ready(
            job_id=job_id,
            job_delivery_id=job_delivery_id,
            request_id=command.request_id,
        )
    except (ManualPrestageJobNotFoundError, ManualPrestageDeliveryNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ManualPrestagePolicyError, ManualPrestageStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManualPrestageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    assert confirmed.supply_mode is not None
    assert confirmed.supply_group_code is not None
    assert confirmed.manual_prestage_ready_at is not None
    assert confirmed.manual_prestage_request_id is not None
    return ManualPrestageCommandResponse(
        job_id=job_id,
        job_delivery_id=confirmed.job_delivery_id,
        supply_mode=confirmed.supply_mode,
        supply_group_code=confirmed.supply_group_code,
        supply_destination_code=confirmed.supply_destination_code,
        manual_prestage_ready_at=confirmed.manual_prestage_ready_at,
        manual_prestage_request_id=confirmed.manual_prestage_request_id,
    )


@router.post(
    "/jobs/{job_id}/steps/{step_id}/execution-ready",
    response_model=OperatorExecutionReadyCommandResponse,
)
def confirm_step_execution_ready(
    job_id: int,
    step_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> OperatorExecutionReadyCommandResponse:
    """Persist physical-clear approval only; FMS remains the sole dispatch authority."""

    try:
        step = OperatorExecutionReadyService(db).confirm(
            job_id=job_id, job_step_id=step_id
        )
    except (
        OperatorExecutionReadyJobNotFoundError,
        OperatorExecutionReadyStepNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        OperatorExecutionReadyStateError,
        OperatorExecutionReadyPolicyError,
        OperatorExecutionReadyDeliveryIncompleteError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    assert step.operator_execution_ready_at is not None
    return OperatorExecutionReadyCommandResponse(
        job_id=job_id,
        job_step_id=step.job_step_id,
        operator_execution_ready_at=step.operator_execution_ready_at,
    )


@router.post(
    "/jobs/{job_id}/outer-walls/operator-ready",
    response_model=OuterWallBatchOperatorReadyResponse,
)
def confirm_outer_walls_operator_ready(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> OuterWallBatchOperatorReadyResponse:
    """Record one operator approval for every current OUTER_WALLS member; FMS dispatch remains sequential."""
    try:
        delivery, steps = OperatorExecutionReadyService(db).confirm_outer_walls_batch(job_id=job_id)
    except OperatorExecutionReadyJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        OperatorExecutionReadyStateError,
        OperatorExecutionReadyPolicyError,
        OperatorExecutionReadyDeliveryIncompleteError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return OuterWallBatchOperatorReadyResponse(
        job_id=job_id,
        job_delivery_id=delivery.job_delivery_id,
        job_step_ids=[step.job_step_id for step in steps],
        authorized_step_ids=[
            step.job_step_id for step in steps
            if step.status is not StepStatus.COMPLETED and step.operator_execution_ready_at is not None
        ],
    )


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/items/{delivery_item_id}/incoming-qa/start",
    response_model=IncomingQAOperatorStartResponse,
)
def start_delivery_item_incoming_qa(
    job_id: int,
    job_delivery_id: int,
    delivery_item_id: int,
    db: Annotated[Session, Depends(get_db)],
    coordinator: Annotated[
        IncomingMaterialQADispatchCoordinator,
        Depends(get_incoming_material_qa_dispatch_coordinator),
    ],
) -> IncomingQAOperatorStartResponse:
    """Operator-start one persisted Incoming QA item; no transport or callback orchestration."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    delivery = db.get(JobMaterialDelivery, job_delivery_id)
    if delivery is None or delivery.production_job_id != job_id:
        raise HTTPException(status_code=404, detail="Job material delivery not found for production job.")
    item = db.get(JobMaterialDeliveryItem, delivery_item_id)
    if item is None or item.job_delivery_id != job_delivery_id:
        raise HTTPException(status_code=404, detail="Job material delivery item not found for delivery.")

    try:
        result = coordinator.dispatch_item(delivery_item_id=delivery_item_id)
    except IncomingMaterialQAHttpTransportError as exc:
        # The coordinator has retained the immutable REQUESTED transaction for a
        # later explicit resend; this response never implies physical QA failure.
        raise HTTPException(status_code=503, detail="Incoming QA Vision request was not acknowledged; state was retained for retry.") from exc
    except IncomingMaterialQAHttpConflictError as exc:
        raise HTTPException(status_code=409, detail="Incoming QA Vision request conflicted with persisted inspection state.") from exc
    except IncomingMaterialQAHttpError as exc:
        raise HTTPException(status_code=502, detail="Incoming QA Vision request failed.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    decision = result.decision
    if decision.action in {
        IncomingMaterialQADispatchAction.NOT_APPLICABLE,
        IncomingMaterialQADispatchAction.POLICY_INVALID,
        IncomingMaterialQADispatchAction.GLOBAL_BUSY,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Incoming QA operator start is not applicable: {decision.action.value}.",
        )
    return IncomingQAOperatorStartResponse(
        action=decision.action.value,
        delivery_item_id=decision.delivery_item_id,
        inspection_request_id=decision.inspection_request_id,
        inspection_cycle=decision.inspection_cycle,
        vision_request_sent=result.acknowledgement is not None,
        acknowledgement_status_code=(
            result.acknowledgement.status_code if result.acknowledgement is not None else None
        ),
    )


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/items/{delivery_item_id}/incoming-qa/reinspect",
    response_model=IncomingQAOperatorStartResponse,
)
def reinspect_delivery_item_incoming_qa(
    job_id: int,
    job_delivery_id: int,
    delivery_item_id: int,
    db: Annotated[Session, Depends(get_db)],
    coordinator: Annotated[
        IncomingMaterialQADispatchCoordinator,
        Depends(get_incoming_material_qa_dispatch_coordinator),
    ],
) -> IncomingQAOperatorStartResponse:
    """Explicitly create the next QA cycle after a terminal material hold."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    delivery = db.get(JobMaterialDelivery, job_delivery_id)
    if delivery is None or delivery.production_job_id != job_id:
        raise HTTPException(status_code=404, detail="Job material delivery not found for production job.")
    item = db.get(JobMaterialDeliveryItem, delivery_item_id)
    if item is None or item.job_delivery_id != job_delivery_id:
        raise HTTPException(status_code=404, detail="Job material delivery item not found for delivery.")

    try:
        result = coordinator.dispatch_reinspection_item(delivery_item_id=delivery_item_id)
    except IncomingMaterialQAHttpTransportError as exc:
        raise HTTPException(status_code=503, detail="Incoming QA Vision reinspection request was not acknowledged; state was retained for retry.") from exc
    except IncomingMaterialQAHttpConflictError as exc:
        raise HTTPException(status_code=409, detail="Incoming QA Vision reinspection request conflicted with persisted inspection state.") from exc
    except IncomingMaterialQAHttpError as exc:
        raise HTTPException(status_code=502, detail="Incoming QA Vision reinspection request failed.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    decision = result.decision
    if decision.action in {
        IncomingMaterialQADispatchAction.NOT_APPLICABLE,
        IncomingMaterialQADispatchAction.POLICY_INVALID,
        IncomingMaterialQADispatchAction.GLOBAL_BUSY,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Incoming QA operator reinspection is not applicable: {decision.action.value}.",
        )
    return IncomingQAOperatorStartResponse(
        action=decision.action.value,
        delivery_item_id=decision.delivery_item_id,
        inspection_request_id=decision.inspection_request_id,
        inspection_cycle=decision.inspection_cycle,
        vision_request_sent=result.acknowledgement is not None,
        acknowledgement_status_code=(
            result.acknowledgement.status_code if result.acknowledgement is not None else None
        ),
    )


@router.post(
    "/jobs/{job_id}/incoming-qa/v02/start",
    response_model=IncomingQAV02PlanResponse,
)
def start_job_incoming_qa_v02(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQAV02PlanResponse:
    """Persist the first v0.2 QA transaction; FMS UDP owns later send I/O."""

    try:
        outcome = IncomingQAV02OrchestrationService(db).start_initial_inspection(job_id=job_id)
    except IncomingQAV02OrchestrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IncomingQAV02PlanResponse(
        created_transaction_ids=list(outcome.created_transaction_ids),
        send_transaction_id=outcome.send_transaction_id,
        blocked_reason=outcome.blocked_reason,
    )


@router.post(
    "/jobs/{job_id}/incoming-qa/v02/reinspect",
    response_model=IncomingQAV02PlanResponse,
)
def reinspect_job_incoming_qa_v02(
    job_id: int,
    command: IncomingQAV02PlanRequest,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQAV02PlanResponse:
    """Plan per-item-cycle v0.2 reinspection groups without HTTP Vision dispatch."""

    try:
        outcome = IncomingQAV02OrchestrationService(db).request_reinspection(
            job_id=job_id,
            delivery_item_ids=command.delivery_item_ids,
        )
    except IncomingQAV02ActiveInspectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IncomingQAV02OrchestrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IncomingQAV02PlanResponse(
        created_transaction_ids=list(outcome.created_transaction_ids),
        send_transaction_id=outcome.send_transaction_id,
        blocked_reason=outcome.blocked_reason,
    )


@router.post(
    "/jobs/{job_id}/incoming-qa/v02/test-hold",
    response_model=IncomingQATestHoldResponse,
)
def set_job_incoming_qa_test_hold(
    job_id: int,
    command: IncomingQATestHoldRequest,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQATestHoldResponse:
    """Persist the pre-production QA test gate; no Vision request is sent here."""

    try:
        enabled = IncomingQAV02OrchestrationService(db).set_test_hold(
            job_id=job_id, enabled=command.enabled
        )
    except IncomingQAV02OrchestrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IncomingQATestHoldResponse(job_id=job_id, incoming_qa_test_hold=enabled)


@router.post(
    "/jobs/{job_id}/incoming-qa/v02/advance-house-b",
    response_model=IncomingQAV02PlanResponse,
)
def advance_job_incoming_qa_house_b_for_test_hold(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQAV02PlanResponse:
    """Plan held HOUSE_B cycle one; FMS reconciliation remains the UDP owner."""

    try:
        outcome = IncomingQAV02OrchestrationService(db).advance_house_b_for_test_hold(job_id=job_id)
    except IncomingQAV02ActiveInspectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IncomingQAV02OrchestrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IncomingQAV02PlanResponse(
        created_transaction_ids=list(outcome.created_transaction_ids),
        send_transaction_id=outcome.send_transaction_id,
        blocked_reason=outcome.blocked_reason,
    )


@router.post(
    "/jobs/{job_id}/incoming-qa/v02/{inspection_mode}/reinspect",
    response_model=IncomingQAV02PlanResponse,
)
def reinspect_job_incoming_qa_v02_mode(
    job_id: int,
    inspection_mode: IncomingQAInspectionMode,
    db: Annotated[Session, Depends(get_db)],
) -> IncomingQAV02PlanResponse:
    """Create one new full durable cycle; FMS owns later UDP transmission."""

    try:
        outcome = IncomingQAV02OrchestrationService(db).request_mode_reinspection(
            job_id=job_id,
            inspection_mode=inspection_mode,
        )
    except IncomingQAV02ActiveInspectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IncomingQAV02OrchestrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return IncomingQAV02PlanResponse(
        created_transaction_ids=list(outcome.created_transaction_ids),
        send_transaction_id=outcome.send_transaction_id,
        blocked_reason=outcome.blocked_reason,
    )


@router.post(
    "/jobs/{job_id}/pre-roof/start",
    response_model=ProductionInspectionResponse,
)
def start_pre_roof_inspection(job_id: int, db: Annotated[Session, Depends(get_db)]) -> ProductionInspection:
    """Create the durable PRE_ROOF inspection; FMS emits its UDP request after commit."""
    try:
        return ProductionCompletionService(db).start_pre_roof_inspection(
            production_job_id=job_id
        )
    except ProductionCompletionNotFoundError as exc:
        raise _job_not_found(job_id) from exc
    except InvalidProductionCompletionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductionCompletionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/jobs/{job_id}/pre-roof/fail",
    response_model=ProductionInspectionResponse,
)
def fail_pre_roof_inspection(
    job_id: int,
    command: PreRoofFailCommandRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionInspection:
    """Temporary manual FAIL control; leaves the Job in PRE_ROOF_READY hold."""
    try:
        return ProductionCompletionService(db).fail_pre_roof_inspection(
            production_job_id=job_id,
            reason=command.failure_reason,
        )
    except ProductionCompletionNotFoundError as exc:
        raise _job_not_found(job_id) from exc
    except InvalidProductionCompletionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductionCompletionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/inspection", response_model=ProductionInspectionResponse | None)
def get_pre_roof_inspection(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionInspection | None:
    """Return the read-only PRE_ROOF inspection runtime, if this job has one."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    return db.scalar(
        select(ProductionInspection)
        .where(
            ProductionInspection.production_job_id == job_id,
            ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
        )
        .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
        .limit(1)
    )


@router.get("/jobs/{job_id}/roof-step", response_model=ProductionJobStepResponse | None)
def get_runtime_roof_step(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionJobStepResponse | None:
    """Return the read-only runtime INSTALL_ROOF JobStep, if inspection passed."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)
    step = db.scalar(
        select(JobStep).where(
            JobStep.job_id == job_id,
            JobStep.operation_code == "INSTALL_ROOF",
        )
    )
    return _to_step_response(step) if step is not None else None


@router.get("/jobs/{job_id}/execution-snapshot", response_model=ProductionExecutionSnapshotResponse)
def get_production_execution_snapshot(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> ProductionExecutionSnapshotResponse:
    """Return one read-only execution/diagnostic snapshot without dispatching work."""

    try:
        return ProductionExecutionSnapshotService(db).get_snapshot(job_id=job_id)
    except ProductionExecutionSnapshotJobNotFoundError as exc:
        raise _job_not_found(job_id) from exc
    except ProductionExecutionSnapshotInconsistencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/execution-attempts/{attempt_id}/error")
def get_execution_attempt_error(attempt_id: int, db: Annotated[Session, Depends(get_db)]) -> dict | None:
    """Read-only authoritative attempt error projection for local observers."""
    return UnityErrorProjectionService(db).get_error_event(attempt_id=attempt_id)


@router.get("/jobs/{job_id}/events", response_model=list[ProductionEventResponse])
def get_production_job_events(
    job_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> list[ProductionEvent]:
    """Return a Job's event history in chronological creation order."""

    if db.get(ProductionJob, job_id) is None:
        raise _job_not_found(job_id)

    statement = (
        select(ProductionEvent)
        .where(ProductionEvent.job_id == job_id)
        .order_by(ProductionEvent.created_at.asc(), ProductionEvent.event_id.asc())
    )
    return list(db.scalars(statement))
