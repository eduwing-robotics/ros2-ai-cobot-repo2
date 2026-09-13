#!/usr/bin/env python3
"""Harmony Incoming QA UDP wire contract v0.2."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


WIRE_VERSION = "0.2"


class InspectionMode(str, Enum):
    BASE_AB = "BASE_AB"
    HOUSE_B = "HOUSE_B"
    HOUSE_A = "HOUSE_A"


class InspectionStatus(str, Enum):
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class InspectionResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class FailureType(str, Enum):
    MISSING = "MISSING"
    WRONG_CLASS = "WRONG_CLASS"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    DEFECT = "DEFECT"
    MULTIPLE_FAILURE = "MULTIPLE_FAILURE"


class CameraSource(str, Enum):
    GLOBAL_CAMERA = "GLOBAL_CAMERA"
    D435 = "D435"


class DefectCode(str, Enum):
    COLOR_NG = "COLOR_NG"
    CRACK_DAMAGE = "CRACK_DAMAGE"
    COMPONENT_MISSING = "COMPONENT_MISSING"
    INCOMPLETE_FORMATION = "INCOMPLETE_FORMATION"


ExactVisionClass = Literal[
    "base_house_a",
    "base_house_b",
    "wall_ext_back_window",
    "wall_ext_door",
    "wall_ext_left_window",
    "wall_ext_right",
    "wall_int_house_a",
    "wall_int_house_a_door",
    "wall_int_house_b",
    "roof_zip",
    "roof_nozip",
]


ALLOWED_SLOTS = {
    InspectionMode.BASE_AB: {
        "C08": "base_house_a",
        "C09": "base_house_b",
    },
    InspectionMode.HOUSE_B: {
        "B01": "wall_ext_back_window",
        "B02": "wall_ext_door",
        "B03": "wall_ext_left_window",
        "B04": "wall_ext_right",
        "B05": "wall_int_house_b",
        "B06": "roof_zip",
    },
    InspectionMode.HOUSE_A: {
        "A01": "wall_ext_back_window",
        "A02": "wall_ext_door",
        "A03": "wall_ext_left_window",
        "A04": "wall_ext_right",
        "A05": "wall_int_house_a",
        "A06": "roof_nozip",
        "A07": "wall_int_house_a_door",
    },
}


class IncomingQaItemRequestV02(BaseModel):
    slot_id: str = Field(min_length=1)
    delivery_item_id: int = Field(ge=1)
    expected_part_code: str = Field(min_length=1)
    expected_class_name: ExactVisionClass
    expected_quantity: int = Field(default=1, ge=1)


class IncomingQaRequestV02(BaseModel):
    ver: Literal["0.2"]
    message_type: Literal["incoming_qa_request"] = "incoming_qa_request"

    inspection_request_id: str = Field(min_length=1)
    inspection_cycle: int = Field(ge=1)
    inspection_mode: InspectionMode

    items: list[IncomingQaItemRequestV02] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_recipe_subset(self):
        allowed = ALLOWED_SLOTS[self.inspection_mode]

        seen = set()

        for item in self.items:
            if item.slot_id in seen:
                raise ValueError(
                    f"duplicate slot_id: {item.slot_id}"
                )

            seen.add(item.slot_id)

            expected = allowed.get(item.slot_id)

            if expected is None:
                raise ValueError(
                    f"{item.slot_id} is not allowed for "
                    f"{self.inspection_mode.value}"
                )

            if item.expected_class_name != expected:
                raise ValueError(
                    f"{item.slot_id} expected_class_name must be "
                    f"{expected}, got {item.expected_class_name}"
                )

        return self


class QualityScoresV02(BaseModel):
    COLOR_NG: float = Field(ge=0.0, le=1.0)
    CRACK_DAMAGE: float = Field(ge=0.0, le=1.0)
    COMPONENT_MISSING: float = Field(ge=0.0, le=1.0)
    INCOMPLETE_FORMATION: float = Field(ge=0.0, le=1.0)


class IncomingQaItemResultV02(BaseModel):
    slot_id: str = Field(min_length=1)
    delivery_item_id: int = Field(ge=1)
    expected_part_code: str = Field(min_length=1)
    expected_class_name: ExactVisionClass
    expected_quantity: int = Field(ge=1)
    detected_quantity: int = Field(ge=0)

    predicted_class_name: ExactVisionClass | None = None
    material_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    result: InspectionResult
    failure_type: FailureType | None = None
    defects: list[DefectCode] = Field(default_factory=list)

    quality_scores: QualityScoresV02 | None = None

    @model_validator(mode="after")
    def validate_item_semantics(self):
        if self.result == InspectionResult.PASS:
            if self.failure_type is not None:
                raise ValueError(
                    "PASS requires failure_type=null"
                )
            if self.defects:
                raise ValueError(
                    "PASS requires defects=[]"
                )

        if self.result == InspectionResult.FAIL:
            if self.failure_type is None:
                raise ValueError(
                    "FAIL requires failure_type"
                )

        if self.result == InspectionResult.NOT_EVALUATED:
            if self.failure_type is not None:
                raise ValueError(
                    "NOT_EVALUATED requires failure_type=null"
                )
            if self.defects:
                raise ValueError(
                    "NOT_EVALUATED requires defects=[]"
                )

        return self


class IncomingQaResultV02(BaseModel):
    ver: Literal["0.2"]
    message_type: Literal["incoming_qa_result"] = "incoming_qa_result"

    inspection_request_id: str = Field(min_length=1)
    inspection_cycle: int = Field(ge=1)
    inspection_mode: InspectionMode

    status: InspectionStatus
    result: InspectionResult | None

    items: list[IncomingQaItemResultV02]

    camera_source: CameraSource
    timestamp: str = Field(min_length=1)

    model_scope: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    production_valid: bool

    @model_validator(mode="after")
    def validate_terminal_semantics(self):
        if self.status == InspectionStatus.COMPLETED:
            if self.result is None:
                raise ValueError(
                    "COMPLETED requires terminal result"
                )

        if self.status == InspectionStatus.ERROR:
            if self.result is not None:
                raise ValueError(
                    "ERROR requires result=null"
                )

        return self
