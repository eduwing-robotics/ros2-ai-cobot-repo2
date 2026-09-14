"""Frozen PRE_ROOF Vision UDP wire contract v0.1.

This module is intentionally independent from Incoming QA.  It validates only
the Request → ACK → five-view Final Result boundary; Team Server owns all
production-valid and roof-gate decisions after a result has been correlated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PRE_ROOF_VIEW_ORDER = ("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")
PRE_ROOF_RUNTIME_VERSION_KEYS = ("controller", *PRE_ROOF_VIEW_ORDER)


class PreRoofMessageType(StrEnum):
    REQUEST = "pre_roof_inspection_request"
    ACK = "pre_roof_inspection_ack"
    RESULT = "pre_roof_inspection_result"


class PreRoofResultCode(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class PreRoofNotEvaluatedReasonCode(StrEnum):
    RUNTIME_NOT_EVALUATED = "RUNTIME_NOT_EVALUATED"
    RUNTIME_ERROR = "RUNTIME_ERROR"


class PreRoofAckReasonCode(StrEnum):
    REQUEST_CONFLICT = "REQUEST_CONFLICT"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC RFC3339.")
    return value


def _require_canonical_uuid(value: UUID | str) -> UUID | str:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("inspection_request_id must be a canonical UUID string.")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("inspection_request_id must be a canonical UUID string.") from exc
    if str(parsed) != value:
        raise ValueError("inspection_request_id must be a canonical UUID string.")
    return value


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreRoofInspectionRequestV01(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.1"] = "0.1"
    message_type: Literal[PreRoofMessageType.REQUEST] = PreRoofMessageType.REQUEST
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    job_id: int = Field(gt=0)
    job_code: str = Field(min_length=1)
    inspection_type: Literal["PRE_ROOF"] = "PRE_ROOF"
    timestamp: datetime

    _timestamp_is_utc = field_validator("timestamp")(_require_utc)


class PreRoofInspectionAckV01(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.1"] = "0.1"
    message_type: Literal[PreRoofMessageType.ACK] = PreRoofMessageType.ACK
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    accepted: bool
    duplicate: bool = False
    reason_code: PreRoofAckReasonCode | None = None

    @model_validator(mode="after")
    def validate_ack_semantics(self) -> "PreRoofInspectionAckV01":
        if self.accepted and self.reason_code is not None:
            raise ValueError("accepted ACK must not include reason_code.")
        if not self.accepted:
            if self.duplicate:
                raise ValueError("rejected ACK cannot be duplicate.")
            if self.reason_code is None:
                raise ValueError("rejected ACK requires reason_code.")
        return self


class PreRoofInspectionViewV01(_WireModel):
    view_name: Literal["TOP", "LEFT", "RIGHT", "FRONT", "BEHIND"]
    result: PreRoofResultCode
    reason_code: PreRoofNotEvaluatedReasonCode | None = None
    defects: list[Any] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_view_semantics(self) -> "PreRoofInspectionViewV01":
        if self.defects:
            raise ValueError("PRE_ROOF v0.1 defects must be exactly [].")
        if self.metrics:
            raise ValueError("PRE_ROOF v0.1 metrics must be exactly {}.")
        if self.result is PreRoofResultCode.NOT_EVALUATED:
            if self.reason_code is None:
                raise ValueError("NOT_EVALUATED view requires reason_code.")
        elif self.reason_code is not None:
            raise ValueError("PASS/FAIL view must not include reason_code.")
        return self


class PreRoofInspectionResultV01(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.1"] = "0.1"
    message_type: Literal[PreRoofMessageType.RESULT] = PreRoofMessageType.RESULT
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    job_id: int = Field(gt=0)
    inspection_type: Literal["PRE_ROOF"] = "PRE_ROOF"
    status: Literal["COMPLETED"] = "COMPLETED"
    overall_result: PreRoofResultCode
    vision_production_valid: bool
    views: list[PreRoofInspectionViewV01] = Field(min_length=5, max_length=5)
    timestamp: datetime
    runtime_profile: Literal["PRE_ROOF_5VIEW"] = "PRE_ROOF_5VIEW"
    runtime_versions: dict[str, str]

    _timestamp_is_utc = field_validator("timestamp")(_require_utc)

    @model_validator(mode="after")
    def validate_final_result(self) -> "PreRoofInspectionResultV01":
        names = tuple(view.view_name for view in self.views)
        if names != PRE_ROOF_VIEW_ORDER:
            raise ValueError("PRE_ROOF views must be exactly TOP, LEFT, RIGHT, FRONT, BEHIND in contract order.")
        if set(self.runtime_versions) != set(PRE_ROOF_RUNTIME_VERSION_KEYS):
            raise ValueError("runtime_versions must contain exactly controller and the five view keys.")
        if any(not isinstance(value, str) or not value.strip() for value in self.runtime_versions.values()):
            raise ValueError("runtime_versions values must be non-empty strings.")
        computed = recompute_pre_roof_overall(self.views)
        if self.overall_result is not computed:
            raise ValueError("overall_result does not match the five-view canonical result.")
        return self


def recompute_pre_roof_overall(views: list[PreRoofInspectionViewV01]) -> PreRoofResultCode:
    results = {view.result for view in views}
    if PreRoofResultCode.NOT_EVALUATED in results:
        return PreRoofResultCode.NOT_EVALUATED
    if PreRoofResultCode.FAIL in results:
        return PreRoofResultCode.FAIL
    return PreRoofResultCode.PASS


# PRE_ROOF v0.2 is deliberately separate from the retained v0.1 archival
# schemas above. The UDP runtime accepts only these per-view messages.
class PreRoofViewName(StrEnum):
    TOP = "TOP"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FRONT = "FRONT"
    BEHIND = "BEHIND"


class PreRoofViewMessageType(StrEnum):
    REQUEST = "pre_roof_view_inspection_request"
    ACK = "pre_roof_view_inspection_ack"
    RESULT = "pre_roof_view_inspection_result"


class PreRoofViewResultCode(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class PreRoofViewInspectionRequestV02(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.2"] = "0.2"
    message_type: Literal[PreRoofViewMessageType.REQUEST] = PreRoofViewMessageType.REQUEST
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    job_id: int = Field(gt=0)
    job_code: str = Field(min_length=1)
    inspection_type: Literal["PRE_ROOF"] = "PRE_ROOF"
    view_name: PreRoofViewName


class PreRoofViewInspectionAckV02(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.2"] = "0.2"
    message_type: Literal[PreRoofViewMessageType.ACK] = PreRoofViewMessageType.ACK
    accepted: bool
    duplicate: bool = False
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    view_name: PreRoofViewName
    reason_code: str | None = None
    timestamp: datetime

    _timestamp_is_utc = field_validator("timestamp")(_require_utc)

    @model_validator(mode="after")
    def validate_ack_semantics(self) -> "PreRoofViewInspectionAckV02":
        if self.accepted and self.reason_code is not None:
            raise ValueError("accepted ACK must not include reason_code.")
        if not self.accepted and self.duplicate:
            raise ValueError("rejected ACK cannot be duplicate.")
        return self


class PreRoofViewInspectionResultV02(_WireModel):
    _request_id_is_canonical = field_validator("inspection_request_id", mode="before")(_require_canonical_uuid)
    ver: Literal["0.2"] = "0.2"
    message_type: Literal[PreRoofViewMessageType.RESULT] = PreRoofViewMessageType.RESULT
    inspection_request_id: UUID
    inspection_cycle: int = Field(ge=1)
    job_id: int = Field(gt=0)
    job_code: str = Field(min_length=1)
    inspection_type: Literal["PRE_ROOF"] = "PRE_ROOF"
    view_name: PreRoofViewName
    result: PreRoofViewResultCode
    runtime_version: str | None = None
    runtime_port: int | None = Field(default=None, ge=1, le=65535)
    production_valid: bool | None = None
    timestamp: datetime

    _timestamp_is_utc = field_validator("timestamp")(_require_utc)
