"""Validated wire schema for AI Perception global vision status packets."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.vision_recipe_mapping import (
    IncomingQAInspectionMode,
    VisionRecipeMappingError,
    expected_vision_class,
)


class VisionStatus(StrEnum):
    """Allowed global camera lifecycle states supplied by AI Perception."""

    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    NO_FRAME = "NO_FRAME"
    ERROR = "ERROR"


class VisionStatusMessage(BaseModel):
    """AI Perception's ``global_vision_status`` UDP payload, version 0.1.

    This model deliberately contains only fields supplied by the Vision PC. The FMS
    receive time is tracked separately in the FMS memory store, not added to this wire
    payload.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"]
    message_type: Literal["global_vision_status"]
    camera_id: str = Field(min_length=1)
    status: VisionStatus
    frame_seq: int = Field(ge=0)
    last_frame_age_sec: float = Field(ge=0)
    inference_error_count: int = Field(ge=0)
    last_error: str | None
    status_stamp_sec: int = Field(ge=0)
    status_stamp_nanosec: int = Field(ge=0)


class IncomingMaterialQARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ver: Literal["0.1"] = "0.1"
    inspection_request_id: str
    delivery_item_id: int
    inspection_cycle: int = Field(ge=1)
    expected_part_code: str
    expected_class_name: str
    expected_quantity: int = Field(ge=1)


class QAWireDetection(BaseModel):
    class_name: str
    class_id: int
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_xyxy: list[float] = Field(min_length=4, max_length=4)


class IncomingMaterialQAResult(BaseModel):
    """Terminal Incoming Material QA result used by the FMS domain service.

    ``status`` defaults to ``COMPLETED`` solely to preserve the pre-v0.1
    in-process test helper construction API. The former public v0.1 HTTP
    callback is retired; this type remains only for internal historical/fake
    service fixtures.
    """

    model_config = ConfigDict(extra="forbid")

    ver: Literal["0.1"] = "0.1"
    inspection_request_id: str
    delivery_item_id: int
    inspection_cycle: int = Field(ge=1)
    status: Literal["COMPLETED", "ERROR"] = "COMPLETED"
    result: Literal["PASS", "FAIL", "NOT_EVALUATED"] | None = None
    failure_type: Literal["MISSING", "WRONG_CLASS", "QUANTITY_MISMATCH", "DEFECT", "MULTIPLE_FAILURE"] | None = None
    expected_part_code: str
    expected_class_name: str
    expected_quantity: int = Field(ge=1)
    detected_quantity: int = Field(ge=0)
    detections: list[QAWireDetection]
    frame_width: int = Field(gt=0)
    frame_height: int = Field(gt=0)
    camera_source: Literal["GLOBAL_CAMERA", "D435"]
    frame_seq: int = Field(ge=0)
    timestamp: datetime
    model_scope: str
    model_version: str
    production_valid: bool

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "IncomingMaterialQAResult":
        if self.status == "COMPLETED" and self.result is None:
            raise ValueError("COMPLETED incoming QA result requires result.")
        if self.status == "ERROR":
            if self.result is not None:
                raise ValueError("ERROR incoming QA result must set result to null.")
            if self.failure_type is not None:
                raise ValueError("ERROR incoming QA result must not include failure_type.")
        return self


class IncomingMaterialQAResultCallback(IncomingMaterialQAResult):
    """Retained legacy v0.1 callback shape; no public route consumes it."""

    status: Literal["COMPLETED", "ERROR"] = Field(...)


# Incoming QA v0.2 is intentionally separate from the retained v0.1 HTTP
# callback types above.  It is a transport-neutral contract for the future
# UDP Request → ACK → Result transaction.
class IncomingQAMessageType(StrEnum):
    REQUEST = "incoming_qa_request"
    ACK = "incoming_qa_ack"
    RESULT = "incoming_qa_result"


class IncomingQAAckReasonCode(StrEnum):
    CONTRACT_CONFLICT = "CONTRACT_CONFLICT"
    ACTIVE_INSPECTION_EXISTS = "ACTIVE_INSPECTION_EXISTS"


class IncomingQADefectCode(StrEnum):
    COLOR_NG = "COLOR_NG"
    CRACK_DAMAGE = "CRACK_DAMAGE"
    COMPONENT_MISSING = "COMPONENT_MISSING"
    INCOMPLETE_FORMATION = "INCOMPLETE_FORMATION"


class IncomingQARequestItemV02(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1)
    delivery_item_id: int = Field(gt=0)
    expected_part_code: str = Field(min_length=1)
    expected_class_name: str = Field(min_length=1)
    expected_quantity: int = Field(gt=0)


class IncomingQARequestV02(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ver: Literal["0.2"] = "0.2"
    message_type: Literal[IncomingQAMessageType.REQUEST] = IncomingQAMessageType.REQUEST
    inspection_request_id: str = Field(min_length=1)
    inspection_cycle: int = Field(gt=0)
    inspection_mode: IncomingQAInspectionMode
    items: list[IncomingQARequestItemV02] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mode_slots_and_duplicates(self) -> "IncomingQARequestV02":
        delivery_item_ids: set[int] = set()
        slot_ids: set[str] = set()
        for item in self.items:
            if item.delivery_item_id in delivery_item_ids:
                raise ValueError("incoming QA v0.2 request cannot repeat delivery_item_id.")
            if item.slot_id in slot_ids:
                raise ValueError("incoming QA v0.2 request cannot repeat slot_id.")
            try:
                configured_class = expected_vision_class(
                    mode=self.inspection_mode, slot_id=item.slot_id
                )
            except VisionRecipeMappingError as exc:
                raise ValueError(str(exc)) from exc
            if item.expected_class_name != configured_class:
                raise ValueError(
                    f"slot {item.slot_id!r} in mode {self.inspection_mode.value} "
                    f"requires expected_class_name={configured_class!r}."
                )
            delivery_item_ids.add(item.delivery_item_id)
            slot_ids.add(item.slot_id)
        return self


class IncomingQAAckV02(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ver: Literal["0.2"] = "0.2"
    message_type: Literal[IncomingQAMessageType.ACK] = IncomingQAMessageType.ACK
    inspection_request_id: str = Field(min_length=1)
    inspection_cycle: int = Field(gt=0)
    accepted: bool
    duplicate: bool = False
    reason_code: IncomingQAAckReasonCode | None = None

    @model_validator(mode="after")
    def validate_ack_semantics(self) -> "IncomingQAAckV02":
        if self.accepted:
            if self.reason_code is not None:
                raise ValueError("accepted ACK must not include reason_code.")
        else:
            if self.duplicate:
                raise ValueError("rejected ACK cannot be duplicate.")
            if self.reason_code is None:
                raise ValueError("rejected ACK requires reason_code.")
        return self


class IncomingQAResultItemV02(IncomingQARequestItemV02):
    predicted_class_name: str | None = Field(default=None, min_length=1)
    material_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detected_quantity: int = Field(ge=0)
    result: Literal["PASS", "FAIL", "NOT_EVALUATED"]
    failure_type: Literal["MISSING", "WRONG_CLASS", "QUANTITY_MISMATCH", "DEFECT", "MULTIPLE_FAILURE"] | None = None
    defects: list[IncomingQADefectCode] = Field(default_factory=list)
    quality_scores: dict[str, float] | None = None

    @model_validator(mode="after")
    def validate_item_result(self) -> "IncomingQAResultItemV02":
        if self.result == "PASS":
            if self.failure_type is not None:
                raise ValueError("PASS item result must not include failure_type.")
        elif self.result == "FAIL":
            if self.failure_type is None:
                raise ValueError("FAIL item result requires failure_type.")
        elif self.failure_type is not None:
            raise ValueError("NOT_EVALUATED item result must not include failure_type.")
        return self


class IncomingQAResultV02(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ver: Literal["0.2"] = "0.2"
    message_type: Literal[IncomingQAMessageType.RESULT] = IncomingQAMessageType.RESULT
    inspection_request_id: str = Field(min_length=1)
    inspection_cycle: int = Field(gt=0)
    inspection_mode: IncomingQAInspectionMode
    status: Literal["COMPLETED"] = "COMPLETED"
    result: Literal["PASS", "FAIL", "NOT_EVALUATED"]
    production_valid: bool
    items: list[IncomingQAResultItemV02] = Field(min_length=1)
    camera_source: str = Field(min_length=1)
    timestamp: datetime
    model_scope: str = Field(min_length=1)
    model_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result_slots_and_duplicates(self) -> "IncomingQAResultV02":
        request = IncomingQARequestV02(
            inspection_request_id=self.inspection_request_id,
            inspection_cycle=self.inspection_cycle,
            inspection_mode=self.inspection_mode,
            items=[
                IncomingQARequestItemV02(
                    slot_id=item.slot_id,
                    delivery_item_id=item.delivery_item_id,
                    expected_part_code=item.expected_part_code,
                    expected_class_name=item.expected_class_name,
                    expected_quantity=item.expected_quantity,
                )
                for item in self.items
            ],
        )
        del request
        return self


def incoming_qa_result_item_evidence_json(item: IncomingQAResultItemV02) -> dict[str, Any]:
    """Return lossless item evidence for persistence, excluding duplicated core columns."""
    return item.model_dump(mode="json")
