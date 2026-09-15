from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, Numeric, Float, String, Text, UniqueConstraint, func, text, Index, CheckConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm.attributes import NO_VALUE
from shared.enums.ai import Intent
from .base import Base
from .datetime_types import AwareDateTime

class PartCategory(StrEnum):
    STRUCTURE="STRUCTURE"
    BATHROOM="BATHROOM"
    KITCHEN="KITCHEN"

class MovementType(StrEnum):
    IN="IN"
    OUT="OUT"
    ADJUST="ADJUST"

class JobStatus(StrEnum):
    REQUESTED="REQUESTED"
    READY="READY"
    RUNNING="RUNNING"
    PAUSED="PAUSED"
    PRE_ROOF_READY="PRE_ROOF_READY"
    ROOF_READY="ROOF_READY"
    COMPLETED="COMPLETED"
    FAILED="FAILED"
    CANCELED="CANCELED"


class ProductionJobControlState(StrEnum):
    """Durable whole-job physical-control lifecycle, separate from JobStatus."""

    ACTIVE = "ACTIVE"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    RESUME_REQUESTED = "RESUME_REQUESTED"

class StepStatus(StrEnum):
    PENDING="PENDING"
    RUNNING="RUNNING"
    COMPLETED="COMPLETED"
    FAILED="FAILED"
    CANCELED="CANCELED"

class InspectionStatus(StrEnum):
    PASS="PASS"
    FAIL="FAIL"

class EventType(StrEnum):
    JOB_CREATED="JOB_CREATED"
    JOB_STARTED="JOB_STARTED"
    JOB_PAUSED="JOB_PAUSED"
    JOB_RESUMED="JOB_RESUMED"
    JOB_CANCELED="JOB_CANCELED"
    PRE_ROOF_READY="PRE_ROOF_READY"
    ROOF_READY="ROOF_READY"
    JOB_COMPLETED="JOB_COMPLETED"
    JOB_FAILED="JOB_FAILED"
    STEP_STARTED="STEP_STARTED"
    STEP_COMPLETED="STEP_COMPLETED"
    STEP_FAILED="STEP_FAILED"
    INSPECTION_STARTED="INSPECTION_STARTED"
    INSPECTION_PASSED="INSPECTION_PASSED"
    INSPECTION_FAILED="INSPECTION_FAILED"
    ROBOT_ERROR="ROBOT_ERROR"
    ROBOT_ERROR_CLEARED="ROBOT_ERROR_CLEARED"
    ROBOT_DISCONNECTED="ROBOT_DISCONNECTED"
    ROBOT_RECONNECTED="ROBOT_RECONNECTED"

class PendingProductionState(StrEnum):
    COLLECTING_DETAILS="COLLECTING_DETAILS"
    WAITING_ROOF_OPTION="WAITING_ROOF_OPTION"
    AWAITING_CONFIRMATION="AWAITING_CONFIRMATION"
    CONFIRMED="CONFIRMED"
    REJECTED="REJECTED"
    EXPIRED="EXPIRED"

class RoofOptionCode(StrEnum):
    ROOF_01="ROOF_01"
    ROOF_02="ROOF_02"

class AIInputType(StrEnum):
    TEXT="TEXT"
    VOICE="VOICE"

class ProductionInspectionType(StrEnum):
    PRE_ROOF="PRE_ROOF"
    FINAL="FINAL"

class ProductionInspectionStatus(StrEnum):
    PENDING="PENDING"
    RUNNING="RUNNING"
    COMPLETED="COMPLETED"
    ERROR="ERROR"


class ProductionInspectionResultCode(StrEnum):
    """Canonical quality outcome for ProductionInspection and each view result."""

    PASS="PASS"
    FAIL="FAIL"
    NOT_EVALUATED="NOT_EVALUATED"

class MaterialDeliveryStatus(StrEnum):
    PENDING="PENDING"
    IN_PROGRESS="IN_PROGRESS"
    COMPLETED="COMPLETED"
    FAILED="FAILED"


class SupplyMode(StrEnum):
    """How one material supply group reaches the Robot Cell.

    This is intentionally independent from the concrete carrier, Robot Cell
    pick zone, and navigation destination implementation.
    """

    TRANSPORTED="TRANSPORTED"
    MANUAL="MANUAL"

class MaterialInspectionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class MaterialInspectionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"

class MaterialInspectionFailureType(StrEnum):
    MISSING = "MISSING"
    WRONG_CLASS = "WRONG_CLASS"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    DEFECT = "DEFECT"
    MULTIPLE_FAILURE = "MULTIPLE_FAILURE"


class IncomingQATransactionStatus(StrEnum):
    """Persisted v0.2 request-level state; UDP runtime is deliberately separate."""

    REQUESTED = "REQUESTED"
    SENT = "SENT"
    ACKED = "ACKED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class CameraSource(StrEnum):
    GLOBAL_CAMERA = "GLOBAL_CAMERA"
    D435 = "D435"

class MaterialFeedStatus(StrEnum):
    PENDING="PENDING"
    RUNNING="RUNNING"
    COMPLETED="COMPLETED"
    FAILED="FAILED"

now = lambda: mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

# ----------------- Core -----------------

class Product(Base):
    __tablename__ = "products"
    product_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = now()

    installation_slots: Mapped[list["InstallationSlot"]] = relationship(back_populates="product")
    jobs: Mapped[list["ProductionJob"]] = relationship(back_populates="product")
    assembly_recipes: Mapped[list["AssemblyRecipe"]] = relationship(back_populates="product")


class Part(Base):
    __tablename__ = "parts"
    part_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    part_name: Mapped[str] = mapped_column(String(150))
    category: Mapped[PartCategory] = mapped_column(SAEnum(PartCategory, name="part_category"))
    vision_class: Mapped[str | None] = mapped_column(String(50))
    unit: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = now()

    inventory: Mapped["Inventory"] = relationship(back_populates="part")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="part")
    recipe_stages: Mapped[list["AssemblyRecipeStage"]] = relationship(back_populates="part")
    job_steps: Mapped[list["JobStep"]] = relationship(
        back_populates="part",
        primaryjoin="Part.part_code == foreign(JobStep.part_code)"
    )


class InstallationSlot(Base):
    __tablename__ = "installation_slots"
    __table_args__ = (
        CheckConstraint("length(trim(slot_code)) > 0", name="ck_installation_slots_code_nonblank"),
    )
    product_code: Mapped[str] = mapped_column(ForeignKey("products.product_code", ondelete="RESTRICT"))
    slot_code: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    product: Mapped[Product] = relationship(back_populates="installation_slots")
    recipe_stages: Mapped[list["AssemblyRecipeStage"]] = relationship(back_populates="installation_slot")
    job_steps: Mapped[list["JobStep"]] = relationship(
        back_populates="installation_slot",
        primaryjoin="InstallationSlot.slot_code == foreign(JobStep.slot_code)"
    )


# ----------------- Recipe -----------------

class AssemblyRecipe(Base):
    __tablename__ = "assembly_recipes"
    __table_args__ = (
        UniqueConstraint("product_code", "version", name="uq_assembly_recipes_product_version"),
        Index("uq_assembly_recipes_active_product", "product_code", unique=True, postgresql_where=text("is_active"), sqlite_where=text("is_active"))
    )
    recipe_id: Mapped[int] = mapped_column(primary_key=True)
    product_code: Mapped[str] = mapped_column(ForeignKey("products.product_code", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="false")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now()

    stages: Mapped[list["AssemblyRecipeStage"]] = relationship(back_populates="recipe")
    jobs: Mapped[list["ProductionJob"]] = relationship(back_populates="assembly_recipe")
    product: Mapped["Product"] = relationship(back_populates="assembly_recipes")


class AssemblyRecipeStage(Base):
    __tablename__ = "assembly_recipe_stages"
    __table_args__ = (
        UniqueConstraint("recipe_id", "stage_order", name="uq_assembly_recipe_stages_order"),
        CheckConstraint("stage_order >= 1", name="ck_assembly_recipe_stages_order_positive"),
        CheckConstraint("quantity IS NULL OR quantity >= 1", name="ck_recipe_stage_quantity_positive")
    )
    recipe_stage_id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("assembly_recipes.recipe_id", ondelete="RESTRICT"))
    stage_order: Mapped[int] = mapped_column(Integer)
    operation_code: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(150))
    part_code: Mapped[str | None] = mapped_column(ForeignKey("parts.part_code", ondelete="RESTRICT"))
    quantity: Mapped[int | None] = mapped_column(Integer)
    slot_code: Mapped[str | None] = mapped_column(ForeignKey("installation_slots.slot_code", ondelete="RESTRICT"))
    pick_zone: Mapped[str | None] = mapped_column(String(100))
    option_code: Mapped[str | None] = mapped_column(String(100))
    execution_gate: Mapped[str | None] = mapped_column(String(100))
    supply_mode: Mapped[SupplyMode | None] = mapped_column(SAEnum(SupplyMode, name="supply_mode"), nullable=True)
    supply_group_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supply_destination_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    recipe: Mapped[AssemblyRecipe] = relationship(back_populates="stages")
    job_steps: Mapped[list["JobStep"]] = relationship(back_populates="source_recipe_stage")
    part: Mapped[Part | None] = relationship(back_populates="recipe_stages")
    installation_slot: Mapped[InstallationSlot | None] = relationship(back_populates="recipe_stages")


# ----------------- Inventory -----------------

class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_inventory_quantity_nonnegative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_inventory_reserved_quantity_nonnegative"),
        CheckConstraint("reserved_quantity <= quantity", name="ck_inventory_reserved_not_above_quantity"),
    )
    part_code: Mapped[str] = mapped_column(ForeignKey("parts.part_code", ondelete="RESTRICT"), primary_key=True)
    # Physical on-hand stock. Reservation never changes this value.
    quantity: Mapped[int] = mapped_column(Integer)
    # Durable allocation to active Jobs that has not entered Cell execution.
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    part: Mapped[Part] = relationship(back_populates="inventory")

    @property
    def available_quantity(self) -> int:
        return self.quantity - self.reserved_quantity


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        # A production Step may issue each part once. NULL remains allowed for
        # ordinary stock movements that have no execution-step source.
        UniqueConstraint("job_step_id", "part_code", "movement_type", name="uq_inventory_movements_step_part_type"),
    )
    movement_id: Mapped[int] = mapped_column(primary_key=True)
    part_code: Mapped[str] = mapped_column(ForeignKey("parts.part_code", ondelete="RESTRICT"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="SET NULL"))
    job_step_id: Mapped[int | None] = mapped_column(ForeignKey("job_steps.job_step_id", ondelete="SET NULL"))
    movement_type: Mapped[MovementType] = mapped_column(SAEnum(MovementType, name="inventory_movement_type"))
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = now()

    part: Mapped[Part] = relationship(back_populates="movements")
    job: Mapped[ProductionJob | None] = relationship(back_populates="movements")
    job_step: Mapped["JobStep | None"] = relationship(back_populates="inventory_movements")


# ----------------- Pending Request -----------------

class PendingProductionRequest(Base):
    __tablename__ = "pending_production_requests"
    __table_args__ = (
        Index("uq_pending_production_requests_active_session", "session_id", unique=True, postgresql_where=text("state IN ('COLLECTING_DETAILS', 'WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')"), sqlite_where=text("state IN ('COLLECTING_DETAILS', 'WAITING_ROOF_OPTION', 'AWAITING_CONFIRMATION')")),
    )
    request_id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[PendingProductionState] = mapped_column(SAEnum(PendingProductionState, name="pending_production_state"))
    product_code: Mapped[str | None] = mapped_column(ForeignKey("products.product_code", ondelete="RESTRICT"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    roof_option_code: Mapped[RoofOptionCode | None] = mapped_column(SAEnum(RoofOptionCode, name="roof_option_code"), nullable=True)
    created_at: Mapped[datetime] = now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list["ProductionJob"]] = relationship(back_populates="source_pending_request")


# ----------------- Production Job -----------------

class ProductionJob(Base):
    __tablename__ = "production_jobs"
    __table_args__ = (
        Index("uq_production_jobs_pending_source_item", "source_pending_request_id", "source_item_index", unique=True, postgresql_where=text("source_pending_request_id IS NOT NULL AND source_item_index IS NOT NULL"), sqlite_where=text("source_pending_request_id IS NOT NULL AND source_item_index IS NOT NULL")),
    )
    job_id: Mapped[int] = mapped_column(primary_key=True)
    job_code: Mapped[str] = mapped_column(String(80), unique=True)
    product_code: Mapped[str] = mapped_column(ForeignKey("products.product_code", ondelete="RESTRICT"))
    assembly_recipe_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_recipes.recipe_id", ondelete="RESTRICT"))
    roof_option_code: Mapped[RoofOptionCode | None] = mapped_column(SAEnum(RoofOptionCode, name="roof_option_code"))
    source_pending_request_id: Mapped[int | None] = mapped_column(ForeignKey("pending_production_requests.request_id", ondelete="SET NULL", use_alter=True))
    source_item_index: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus, name="job_status"))
    requested_at: Mapped[datetime] = now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Set only when unused job reservations are durably released at terminal failure/cancel.
    inventory_reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Pause control is independent from business JobStatus until the physical
    # executor has actually confirmed its HELD/resumed state.
    control_state: Mapped[ProductionJobControlState] = mapped_column(
        SAEnum(ProductionJobControlState, name="production_job_control_state"),
        nullable=False,
        default=ProductionJobControlState.ACTIVE,
        server_default=ProductionJobControlState.ACTIVE.value,
    )
    control_req_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    control_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Durable requested PAUSE behavior, not physical-stop evidence. The FMS
    # reads this while PAUSE_REQUESTED survives a restart and clears it after
    # the request has settled or has been superseded by resume.
    control_immediate_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    # Operator-controlled pre-production QA test gate. This is intentionally
    # independent from physical pause/resume control and survives restarts.
    incoming_qa_test_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    product: Mapped[Product] = relationship(back_populates="jobs")
    steps: Mapped[list["JobStep"]] = relationship(back_populates="job")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="job")
    events: Mapped[list["ProductionEvent"]] = relationship(back_populates="job")
    lifecycle_inspections: Mapped[list["ProductionInspection"]] = relationship(back_populates="production_job")
    deliveries: Mapped[list["JobMaterialDelivery"]] = relationship(back_populates="production_job")
    source_pending_request: Mapped["PendingProductionRequest | None"] = relationship(foreign_keys=[source_pending_request_id], post_update=True)
    assembly_recipe: Mapped["AssemblyRecipe | None"] = relationship(back_populates="jobs")

    @property
    def assembly_recipe_version(self) -> int | None:
        return self.assembly_recipe.version if self.assembly_recipe else None


class JobStep(Base):
    __tablename__ = "job_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "step_order", name="uq_job_step_order"),
        UniqueConstraint("job_id", "source_recipe_stage_id", name="uq_job_steps_job_source_recipe_stage"),
        CheckConstraint("quantity IS NULL OR quantity >= 1", name="ck_job_step_quantity_positive")
    )
    job_step_id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="RESTRICT"))
    source_recipe_stage_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_recipe_stages.recipe_stage_id", ondelete="SET NULL"))
    step_order: Mapped[int] = mapped_column(Integer)
    operation_code: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(150))

    part_code: Mapped[str | None] = mapped_column(String(50))
    quantity: Mapped[int | None] = mapped_column(Integer)
    slot_code: Mapped[str | None] = mapped_column(String(100))
    pick_zone: Mapped[str | None] = mapped_column(String(100))
    roof_option_code: Mapped[str | None] = mapped_column(String(100))
    vision_class: Mapped[str | None] = mapped_column(String(50))

    supply_mode: Mapped[SupplyMode | None] = mapped_column(SAEnum(SupplyMode, name="supply_mode"), nullable=True)
    supply_group_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supply_destination_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    status: Mapped[StepStatus] = mapped_column(SAEnum(StepStatus, name="job_step_status"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Durable human release after TRANSPORTED delivery and before Cell dispatch.
    operator_execution_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only after a Robot Cell Goal has been accepted and this Step's reserved
    # material is issued from physical on-hand stock.
    inventory_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


    job: Mapped[ProductionJob] = relationship(back_populates="steps")
    source_recipe_stage: Mapped["AssemblyRecipeStage | None"] = relationship(back_populates="job_steps")
    part: Mapped[Part | None] = relationship(
        back_populates="job_steps",
        primaryjoin="foreign(JobStep.part_code) == Part.part_code"
    )
    installation_slot: Mapped["InstallationSlot | None"] = relationship(
        back_populates="job_steps",
        primaryjoin="foreign(JobStep.slot_code) == InstallationSlot.slot_code"
    )
    events: Mapped[list["ProductionEvent"]] = relationship(back_populates="job_step")
    delivery_items: Mapped[list["JobMaterialDeliveryItem"]] = relationship(back_populates="job_step")
    inventory_movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="job_step")

    @property
    def resolved_step_order(self) -> int:
        return self.step_order
    @property
    def resolved_step_code(self) -> str:
        return self.operation_code
    @property
    def resolved_display_name(self) -> str:
        return self.display_name


class ProductionEvent(Base):
    __tablename__ = "production_events"
    event_id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="SET NULL"))
    job_step_id: Mapped[int | None] = mapped_column(ForeignKey("job_steps.job_step_id", ondelete="SET NULL"))
    event_type: Mapped[EventType] = mapped_column(SAEnum(EventType, name="production_event_type"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = now()

    job: Mapped[ProductionJob | None] = relationship(back_populates="events")
    job_step: Mapped[JobStep | None] = relationship(back_populates="events")


class ProductionInspection(Base):
    __tablename__ = "production_inspections"
    __table_args__ = (
        CheckConstraint("inspection_cycle >= 1", name="ck_production_inspections_cycle_positive"),
        UniqueConstraint(
            "production_job_id",
            "inspection_type",
            "inspection_cycle",
            name="uq_production_inspections_job_type_cycle",
        ),
        UniqueConstraint("inspection_request_id", name="uq_production_inspections_request_id"),
        CheckConstraint(
            "(status IN ('PENDING', 'RUNNING', 'ERROR') AND result IS NULL AND production_valid = false) "
            "OR (status = 'COMPLETED' AND result IN ('PASS', 'FAIL', 'NOT_EVALUATED') "
            "AND (result = 'PASS' OR production_valid = false))",
            name="ck_production_inspections_status_result_valid",
        ),
        CheckConstraint(
            "(status IN ('PENDING', 'RUNNING', 'ERROR') AND is_passed IS NULL) "
            "OR (status = 'COMPLETED' AND result = 'PASS' AND is_passed = true) "
            "OR (status = 'COMPLETED' AND result = 'FAIL' AND is_passed = false) "
            "OR (status = 'COMPLETED' AND result = 'NOT_EVALUATED' AND is_passed IS NULL)",
            name="ck_production_inspections_legacy_is_passed",
        ),
    )
    inspection_id: Mapped[int] = mapped_column(primary_key=True)
    production_job_id: Mapped[int] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="RESTRICT"))
    inspection_type: Mapped[ProductionInspectionType] = mapped_column(SAEnum(ProductionInspectionType, name="production_inspection_type"))
    inspection_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ProductionInspectionStatus] = mapped_column(SAEnum(ProductionInspectionStatus, name="production_inspection_status"))
    inspection_request_id: Mapped[str] = mapped_column(String(36), nullable=False, default=lambda: str(uuid4()))
    result: Mapped[ProductionInspectionResultCode | None] = mapped_column(
        SAEnum(ProductionInspectionResultCode, name="production_inspection_result"), nullable=True
    )
    # Vision readiness is evidence supplied by a future PRE_ROOF wire runtime;
    # it is deliberately not the Team Server's roof-gate authority.
    vision_production_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    production_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Persisted wire evidence makes retries/result idempotency restart-safe.
    # These stay NULL for legacy/manual inspections that never use UDP.
    wire_request_snapshot_json: Mapped[str | None] = mapped_column(Text)
    wire_request_digest: Mapped[str | None] = mapped_column(String(64))
    wire_result_digest: Mapped[str | None] = mapped_column(String(64))
    wire_error_code: Mapped[str | None] = mapped_column(String(100))
    wire_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wire_acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wire_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    runtime_profile: Mapped[str | None] = mapped_column(String(100))
    runtime_versions_json: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(AwareDateTime(), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Deprecated compatibility projection only: PASS=True, FAIL=False, and
    # NOT_EVALUATED/active/error=None. ``result`` is canonical.
    is_passed: Mapped[bool | None] = mapped_column(Boolean)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    production_job: Mapped[ProductionJob] = relationship(back_populates="lifecycle_inspections")
    results: Mapped[list["ProductionInspectionResult"]] = relationship(back_populates="inspection")

    view_requests: Mapped[list["ProductionInspectionViewRequest"]] = relationship(back_populates="inspection")

@event.listens_for(ProductionInspection.inspection_request_id, "set", retval=True)
def _prevent_persisted_production_inspection_request_id_change(target, value, oldvalue, initiator):
    """Wire correlation identity is immutable after its inspection row is persisted."""
    if (
        inspect(target).persistent
        and oldvalue is not NO_VALUE
        and oldvalue is not None
        and value != oldvalue
    ):
        raise ValueError("ProductionInspection.inspection_request_id is immutable once persisted.")
    return value


class ProductionInspectionResult(Base):
    __tablename__ = "production_inspection_results"
    result_id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("production_inspections.inspection_id", ondelete="RESTRICT"))
    item_code: Mapped[str] = mapped_column(String(100))
    item_name: Mapped[str] = mapped_column(String(150))
    result: Mapped[ProductionInspectionResultCode] = mapped_column(
        SAEnum(ProductionInspectionResultCode, name="production_inspection_result"), nullable=False
    )
    # Deprecated compatibility projection; NOT_EVALUATED is represented by NULL.
    is_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    measured_value: Mapped[str | None] = mapped_column(String(200))
    expected_value: Mapped[str | None] = mapped_column(String(200))
    tolerance_info: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = now()

    inspection: Mapped[ProductionInspection] = relationship(back_populates="results")


class ProductionInspectionViewRequest(Base):
    """Durable PRE_ROOF v0.2 per-view wire request history."""

    __tablename__ = "production_inspection_view_requests"
    __table_args__ = (
        UniqueConstraint("inspection_request_id", name="uq_pre_roof_view_requests_request_id"),
        CheckConstraint("view_name IN ('TOP', 'LEFT', 'RIGHT', 'FRONT', 'BEHIND')", name="ck_pre_roof_view_requests_view_name"),
        CheckConstraint("status IN ('REQUESTED', 'SENT', 'ACKED', 'COMPLETED', 'FAILED')", name="ck_pre_roof_view_requests_status"),
        CheckConstraint("retry_count >= 0", name="ck_pre_roof_view_requests_retry_count"),
    )
    view_request_id: Mapped[int] = mapped_column(primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("production_inspections.inspection_id", ondelete="RESTRICT"), nullable=False)
    view_name: Mapped[str] = mapped_column(String(10), nullable=False)
    inspection_request_id: Mapped[str] = mapped_column(String(36), nullable=False, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="REQUESTED")
    result: Mapped[ProductionInspectionResultCode | None] = mapped_column(
        SAEnum(ProductionInspectionResultCode, name="production_inspection_result"), nullable=True
    )
    request_snapshot_json: Mapped[str | None] = mapped_column(Text)
    request_digest: Mapped[str | None] = mapped_column(String(64))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    runtime_version: Mapped[str | None] = mapped_column(String(100))
    runtime_port: Mapped[int | None] = mapped_column(Integer)
    vision_production_valid: Mapped[bool | None] = mapped_column(Boolean)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    inspection: Mapped[ProductionInspection] = relationship(back_populates="view_requests")

# ----------------- Material Flow -----------------

class JobMaterialDelivery(Base):
    __tablename__ = "job_material_deliveries"
    __table_args__ = (
        UniqueConstraint("production_job_id", "supply_group_code", name="uq_job_material_deliveries_job_supply_group"),
    )
    job_delivery_id: Mapped[int] = mapped_column(primary_key=True)
    production_job_id: Mapped[int] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="RESTRICT"))
    batch_order: Mapped[int] = mapped_column(Integer)
    delivery_code: Mapped[str] = mapped_column(String(80))
    display_name: Mapped[str] = mapped_column(String(150))
    status: Mapped[MaterialDeliveryStatus] = mapped_column(SAEnum(MaterialDeliveryStatus, name="material_delivery_status"))
    created_at: Mapped[datetime] = now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    supply_mode: Mapped[SupplyMode | None] = mapped_column(SAEnum(SupplyMode, name="supply_mode"), nullable=True)
    supply_group_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supply_destination_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    physical_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    physical_ready_request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # MANUAL supply uses a separate human-at-cell prestage fact. It must never
    # be confused with TRANSPORTED pallet preparation before TurtleBot transport.
    manual_prestage_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_prestage_request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    production_job: Mapped[ProductionJob] = relationship(back_populates="deliveries")
    items: Mapped[list["JobMaterialDeliveryItem"]] = relationship(back_populates="job_delivery")
    feed_execution: Mapped["JobMaterialFeedExecution | None"] = relationship(back_populates="job_delivery", uselist=False)


class JobMaterialDeliveryItem(Base):
    __tablename__ = "job_material_delivery_items"
    __table_args__ = (
        UniqueConstraint("job_delivery_id", "job_step_id", "part_code", name="uq_job_material_delivery_items_step_part"),
        CheckConstraint("quantity >= 1", name="ck_job_material_delivery_items_quantity")
    )
    delivery_item_id: Mapped[int] = mapped_column(primary_key=True)
    job_delivery_id: Mapped[int] = mapped_column(ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"))
    # A delayed material-bearing RecipeStage can exist for Incoming QA before
    # its execution-gated JobStep is materialized.  The FK remains intact; only
    # the pre-PRE_ROOF binding is temporarily NULL.
    job_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_steps.job_step_id", ondelete="RESTRICT"), nullable=True
    )
    source_recipe_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("assembly_recipe_stages.recipe_stage_id", ondelete="RESTRICT"), nullable=True
    )
    part_code: Mapped[str] = mapped_column(ForeignKey("parts.part_code", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer)
    is_delivered: Mapped[bool] = mapped_column(Boolean, server_default="false")

    job_delivery: Mapped[JobMaterialDelivery] = relationship(back_populates="items")
    job_step: Mapped["JobStep | None"] = relationship(back_populates="delivery_items")
    part: Mapped[Part] = relationship()


class JobMaterialFeedExecution(Base):
    __tablename__ = "job_material_feed_executions"
    feed_execution_id: Mapped[int] = mapped_column(primary_key=True)
    job_delivery_id: Mapped[int] = mapped_column(ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"), unique=True)
    status: Mapped[MaterialFeedStatus] = mapped_column(SAEnum(MaterialFeedStatus, name="material_feed_status"))
    created_at: Mapped[datetime] = now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    completed_json: Mapped[str] = mapped_column(Text, server_default="'[]'")

    job_delivery: Mapped[JobMaterialDelivery] = relationship(back_populates="feed_execution")

class IncomingQATransaction(Base):
    """One immutable Vision Incoming-QA v0.2 request spanning one or more items.

    This is persistence only.  UDP dispatch, ACK processing, retries, and result
    listening are intentionally not wired in this foundation.
    """

    __tablename__ = "incoming_qa_transactions"
    __table_args__ = (
        CheckConstraint("inspection_cycle >= 1", name="ck_incoming_qa_transactions_cycle"),
        CheckConstraint("retry_count >= 0", name="ck_incoming_qa_transactions_retry_count"),
    )

    transaction_id: Mapped[int] = mapped_column(primary_key=True)
    inspection_request_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    production_job_id: Mapped[int] = mapped_column(
        ForeignKey("production_jobs.job_id", ondelete="RESTRICT"), nullable=False
    )
    inspection_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    inspection_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[IncomingQATransactionStatus] = mapped_column(
        SAEnum(IncomingQATransactionStatus, name="incoming_qa_transaction_status"),
        nullable=False,
    )
    ack_accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ack_duplicate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ack_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Transport/runtime failures are distinct from a Vision ACK rejection.
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    overall_result: Mapped[MaterialInspectionResult | None] = mapped_column(
        SAEnum(MaterialInspectionResult, name="material_inspection_result"), nullable=True
    )
    production_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    immutable_request_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    # Canonical terminal evidence allows exact replay to be idempotent and
    # conflicting terminal replays to be rejected without overwriting history.
    result_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    camera_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vision_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_scope: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    production_job: Mapped[ProductionJob] = relationship()
    inspections: Mapped[list["MaterialInspection"]] = relationship(
        back_populates="incoming_qa_transaction"
    )


class MaterialInspection(Base):
    __tablename__ = "material_inspections"
    __table_args__ = (
        UniqueConstraint("delivery_item_id", "inspection_cycle", name="uq_material_inspections_cycle"),
        UniqueConstraint(
            "incoming_qa_transaction_id",
            "delivery_item_id",
            name="uq_material_inspections_transaction_item",
        ),
        CheckConstraint("inspection_cycle >= 1", name="ck_material_inspections_cycle"),
        CheckConstraint(
            "material_confidence IS NULL OR (material_confidence >= 0 AND material_confidence <= 1)",
            name="ck_material_inspections_material_confidence",
        ),
    )
    inspection_id: Mapped[int] = mapped_column(primary_key=True)
    # v0.1 rows remain single-item; v0.2 rows deliberately share this ID per transaction.
    inspection_request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    incoming_qa_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("incoming_qa_transactions.transaction_id", ondelete="RESTRICT"), nullable=True
    )
    delivery_item_id: Mapped[int] = mapped_column(ForeignKey("job_material_delivery_items.delivery_item_id", ondelete="RESTRICT"), nullable=False)
    inspection_cycle: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[MaterialInspectionStatus] = mapped_column(SAEnum(MaterialInspectionStatus, name="material_inspection_status"), nullable=False)
    result: Mapped[MaterialInspectionResult | None] = mapped_column(SAEnum(MaterialInspectionResult, name="material_inspection_result"), nullable=True)
    failure_type: Mapped[MaterialInspectionFailureType | None] = mapped_column(SAEnum(MaterialInspectionFailureType, name="material_inspection_failure_type"), nullable=True)

    expected_part_code: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    detected_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detections_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Queryable primary Vision classification values for v0.2 item results.
    predicted_class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    material_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Lossless v0.2 item evidence (defects, quality_scores, and future debug data).
    result_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    frame_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    camera_source: Mapped[CameraSource | None] = mapped_column(SAEnum(CameraSource, name="camera_source"), nullable=True)
    frame_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vision_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    model_scope: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    production_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_at: Mapped[datetime] = now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    delivery_item: Mapped[JobMaterialDeliveryItem] = relationship(backref="inspections")
    incoming_qa_transaction: Mapped[IncomingQATransaction | None] = relationship(
        back_populates="inspections"
    )

class ExecutorType(StrEnum):
    ROBOT_CELL = "ROBOT_CELL"
    FORKLIFT = "FORKLIFT"

class ExecutionAttemptStatus(StrEnum):
    CREATED = "CREATED"
    DISPATCHING = "DISPATCHING"
    ACCEPTED = "ACCEPTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


class ExecutionAttemptControlState(StrEnum):
    """Durable physical-control state; an accepted Action may simultaneously be HELD."""

    ACTIVE = "ACTIVE"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    HELD = "HELD"
    RESUME_REQUESTED = "RESUME_REQUESTED"

class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="ck_execution_attempts_no_positive"),
        Index("ix_execution_attempts_job_step_id_command", "job_step_id", "command_type"),
        Index("ix_execution_attempts_job_delivery_id_command", "job_delivery_id", "command_type"),
        Index("ix_execution_attempts_status", "status"),
    )
    attempt_id: Mapped[int] = mapped_column(primary_key=True)
    req_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    executor_type: Mapped[ExecutorType] = mapped_column(SAEnum(ExecutorType, name="executor_type"), nullable=False)
    command_type: Mapped[str] = mapped_column(String(50), nullable=False)

    job_id: Mapped[int | None] = mapped_column(ForeignKey("production_jobs.job_id", ondelete="RESTRICT"), nullable=True)
    job_step_id: Mapped[int | None] = mapped_column(ForeignKey("job_steps.job_step_id", ondelete="RESTRICT"), nullable=True)
    job_delivery_id: Mapped[int | None] = mapped_column(ForeignKey("job_material_deliveries.job_delivery_id", ondelete="RESTRICT"), nullable=True)

    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ExecutionAttemptStatus] = mapped_column(SAEnum(ExecutionAttemptStatus, name="execution_attempt_status"), nullable=False)

    request_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    goal_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Separate from terminal Action lifecycle: ExecuteTask stays ACCEPTED while
    # the Robot Cell is HELD.
    control_state: Mapped[ExecutionAttemptControlState] = mapped_column(
        SAEnum(ExecutionAttemptControlState, name="execution_attempt_control_state"),
        nullable=False,
        default=ExecutionAttemptControlState.ACTIVE,
        server_default=ExecutionAttemptControlState.ACTIVE.value,
    )
    control_req_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    control_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    control_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
