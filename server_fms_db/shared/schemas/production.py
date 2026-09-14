"""Read-only response schemas for production monitoring."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from shared.models.factory import (
    CameraSource,
    EventType,
    ExecutionAttemptStatus,
    IncomingQATransactionStatus,
    JobStatus,
    MaterialDeliveryStatus,
    MaterialFeedStatus,
    MaterialInspectionFailureType,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    ProductionInspectionType,
    ProductionJobControlState,
    RoofOptionCode,
    StepStatus,
    SupplyMode,
)


class ProductionJobSummaryResponse(BaseModel):
    job_id: int
    job_code: str
    product_code: str
    roof_option_code: RoofOptionCode | None
    assembly_recipe_id: int | None
    assembly_recipe_version: int | None
    source_pending_request_id: int | None
    source_item_index: int | None
    status: JobStatus
    control_state: ProductionJobControlState
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ProductionJobCancelResponse(BaseModel):
    job_id: int
    job_code: str
    status: JobStatus

    model_config = ConfigDict(from_attributes=True)


class ProductionJobStepResponse(BaseModel):
    job_step_id: int
    step_order: int
    step_code: str
    step_name: str
    operation_code: str | None
    source_recipe_stage_id: int | None
    supply_mode: SupplyMode | None
    status: StepStatus
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None
    operator_execution_ready_at: datetime | None


class OperatorExecutionReadyCommandResponse(BaseModel):
    """Durable operator approval only; this response never represents dispatch."""

    job_id: int
    job_step_id: int
    operator_execution_ready_at: datetime


class OuterWallBatchOperatorReadyResponse(BaseModel):
    job_id: int
    job_delivery_id: int
    job_step_ids: list[int]
    authorized_step_ids: list[int]


class JobMaterialFeedExecutionResponse(BaseModel):
    feed_execution_id: int
    job_delivery_id: int
    status: MaterialFeedStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    error_code: str | None
    failure_reason: str | None
    completed_json: str

    model_config = ConfigDict(from_attributes=True)


class IncomingQAMonitoringState(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    RELEASED = "RELEASED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_RELEASED = "NOT_RELEASED"


class IncomingQALatestInspectionResponse(BaseModel):
    inspection_request_id: str
    inspection_cycle: int
    status: MaterialInspectionStatus
    result: MaterialInspectionResult | None
    production_valid: bool | None
    failure_type: MaterialInspectionFailureType | None
    failure_reason: str | None
    detected_quantity: int | None
    camera_source: CameraSource | None
    vision_timestamp: datetime | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobMaterialDeliveryItemResponse(BaseModel):
    delivery_item_id: int
    # A deferred Roof material item legitimately exists before PRE_ROOF PASS.
    # It is bound to its runtime JobStep only after that production gate.
    job_step_id: int | None
    part_code: str
    vision_class: str | None
    expected_quantity: int
    qa_state: IncomingQAMonitoringState
    qa_released: bool
    latest_inspection: IncomingQALatestInspectionResponse | None


class JobMaterialDeliveryResponse(BaseModel):
    job_delivery_id: int
    batch_order: int
    delivery_code: str
    display_name: str
    status: MaterialDeliveryStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    supply_mode: SupplyMode | None
    supply_group_code: str | None
    supply_destination_code: str | None
    physical_ready_at: datetime | None
    physical_ready_request_id: str | None
    physical_ready: bool
    manual_prestage_ready_at: datetime | None
    manual_prestage_ready: bool
    qa_applicable: bool | None
    qa_total_items: int
    qa_released_items: int
    qa_all_released: bool
    empty_pallet_return_status: str
    can_empty_pallet_return: bool
    terminal_drop_cleanup_required: bool
    can_terminal_drop_cleanup: bool
    can_start_outer_wall_batch: bool
    items: list[JobMaterialDeliveryItemResponse]
    feed_execution: JobMaterialFeedExecutionResponse | None

    model_config = ConfigDict(from_attributes=True)


class IncomingQAOperatorStartResponse(BaseModel):
    """Outcome of an operator-requested send/reuse for one persisted QA item."""

    action: str
    delivery_item_id: int
    inspection_request_id: str | None
    inspection_cycle: int | None
    vision_request_sent: bool
    acknowledgement_status_code: int | None


class IncomingQAV02InspectionItemResponse(BaseModel):
    """Read-only v0.2 item evidence, including unresolved requested items."""

    slot_id: str
    delivery_item_id: int
    expected_part_code: str
    expected_class_name: str
    expected_quantity: int
    inspection_cycle: int
    status: MaterialInspectionStatus | None
    result: MaterialInspectionResult | None
    production_valid: bool | None
    predicted_class_name: str | None
    material_confidence: float | None
    detected_quantity: int | None
    failure_type: MaterialInspectionFailureType | None
    failure_reason: str | None
    defects: list[str]
    quality_scores: dict[str, float] | None
    requested_at: datetime | None
    completed_at: datetime | None


class IncomingQAV02TransactionResponse(BaseModel):
    """Read-only persisted request-level Incoming QA v0.2 diagnostics."""

    transaction_id: int
    inspection_request_id: str
    inspection_mode: str
    inspection_cycle: int
    status: IncomingQATransactionStatus
    overall_result: MaterialInspectionResult | None
    production_valid: bool | None
    retry_count: int
    ack_accepted: bool | None
    ack_duplicate: bool | None
    ack_reason_code: str | None
    error_reason: str | None
    camera_source: str | None
    vision_timestamp: datetime | None
    model_scope: str | None
    model_version: str | None
    created_at: datetime
    sent_at: datetime | None
    acked_at: datetime | None
    completed_at: datetime | None
    can_reinspect_mode: bool = False
    items: list[IncomingQAV02InspectionItemResponse]


class IncomingQAV02GateResponse(BaseModel):
    """Existing latest-effective Incoming QA gate, rendered for diagnostics only."""

    status: str
    total_expected_items: int
    released_items: int


class IncomingQAV02MonitorResponse(BaseModel):
    """Bounded, zero-write diagnostic projection for one production Job."""

    job_id: int
    gate: IncomingQAV02GateResponse
    incoming_qa_test_hold: bool = False
    can_enable_incoming_qa_test_hold: bool = False
    can_disable_incoming_qa_test_hold: bool = False
    can_advance_house_b: bool = False
    can_release_incoming_qa_test_hold: bool = False
    transactions: list[IncomingQAV02TransactionResponse]


class IncomingQAV02PlanRequest(BaseModel):
    """Operator-selected item set for v0.2 reinspection planning only."""

    delivery_item_ids: list[int] = Field(min_length=1)


class IncomingQATestHoldRequest(BaseModel):
    """Durable per-job Incoming QA test hold command."""

    enabled: bool


class IncomingQATestHoldResponse(BaseModel):
    job_id: int
    incoming_qa_test_hold: bool


class IncomingQAV02PlanResponse(BaseModel):
    created_transaction_ids: list[int]
    send_transaction_id: int | None
    blocked_reason: str | None
    udp_dispatched: bool = False


class PhysicalReadyCommandRequest(BaseModel):
    """Operator assertion only; policy and timestamps remain server-owned."""

    request_id: str = Field(min_length=1, max_length=100)


class PhysicalReadyCommandResponse(BaseModel):
    job_id: int
    job_delivery_id: int
    supply_mode: SupplyMode
    supply_group_code: str
    supply_destination_code: str | None
    physical_ready_at: datetime
    physical_ready_request_id: str


class ManualPrestageCommandResponse(BaseModel):
    job_id: int
    job_delivery_id: int
    supply_mode: SupplyMode
    supply_group_code: str
    supply_destination_code: str | None
    manual_prestage_ready_at: datetime
    manual_prestage_request_id: str


class ManualTransportRecoveryRequest(BaseModel):
    """Operator reports a physically observed endpoint, never a DB status."""

    attempt_id: int = Field(gt=0)
    confirmed_location_code: str = Field(min_length=1, max_length=100)
    operator_note: str | None = Field(default=None, max_length=1000)

    model_config = ConfigDict(extra="forbid")


class ManualTransportRecoveryResponse(BaseModel):
    job_id: int
    delivery_id: int
    attempt_id: int
    command_type: str
    confirmed_location_code: str
    attempt_status: ExecutionAttemptStatus
    delivery_status: MaterialDeliveryStatus
    derived_drop_state: str
    recovery_applied: bool


class EmptyPalletReturnCommandRequest(BaseModel):
    """An operator confirms the physical fact; the server owns all transitions."""

    confirmed_empty: bool

    model_config = ConfigDict(extra="forbid")


class EmptyPalletReturnCommandResponse(BaseModel):
    job_id: int
    job_delivery_id: int
    attempt_id: int
    request_id: str
    pickup_code: str
    dropoff_code: str
    status: str


class PreRoofFailCommandRequest(BaseModel):
    """Optional operator context; inspection and Job states remain server-owned."""

    failure_reason: str | None = Field(default=None, min_length=1, max_length=1000)


class ProductionInspectionViewResponse(BaseModel):
    view_name: str
    status: str
    result: ProductionInspectionResultCode | None = None


class ProductionInspectionResponse(BaseModel):
    inspection_id: int
    inspection_type: ProductionInspectionType
    inspection_cycle: int
    inspection_request_id: str
    status: ProductionInspectionStatus
    result: ProductionInspectionResultCode | None
    vision_production_valid: bool
    production_valid: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None
    # Additive v0.2 PRE_ROOF aggregate fields. Older callers retain the
    # original inspection fields unchanged.
    current_view: str | None = None
    views: list[ProductionInspectionViewResponse] = Field(default_factory=list)
    gate_state: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ProductionJobDetailResponse(ProductionJobSummaryResponse):
    # Additive canonical PROCESS projection for monitoring/Voice consumers.
    process_stage_code: str | None = None
    process_stage_order: int | None = None
    process_stage_display_name: str | None = None
    steps: list[ProductionJobStepResponse]
    current_step: ProductionJobStepResponse | None


class ProductionEventResponse(BaseModel):
    event_id: int
    event_type: EventType
    job_id: int | None
    job_step_id: int | None

    error_code: str | None
    message: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExecutionStepSnapshotResponse(ProductionJobStepResponse):
    """A JobStep rendered for execution diagnostics, not an execution command."""

    ready: bool | None
    readiness_reason: str | None
    dispatchable: bool | None
    dispatch_block_reason: str | None


class ProductionRoofSnapshotResponse(BaseModel):
    roof_option_code: RoofOptionCode | None
    step: ExecutionStepSnapshotResponse | None


class ExecutionEventSnapshotResponse(BaseModel):
    event_id: int
    event_type: EventType
    job_step_id: int | None
    error_code: str | None
    message: str
    created_at: datetime


class ProductionExecutionSnapshotResponse(BaseModel):
    job_id: int
    job_status: JobStatus
    control_state: ProductionJobControlState
    product_code: str
    current_step: ExecutionStepSnapshotResponse | None
    next_step: ExecutionStepSnapshotResponse | None
    inspection: ProductionInspectionResponse | None
    roof: ProductionRoofSnapshotResponse
    last_event: ExecutionEventSnapshotResponse | None
    last_step_execution_event: ExecutionEventSnapshotResponse | None

class TransportStatusData(BaseModel):
    req_id: str
    job_id: int | None
    delivery_id: int | None
    robot_id: str
    task_type: str
    phase: str
    progress: float
    result: str | None = None
    error_code: str | None = None
    detail: str | None = None
