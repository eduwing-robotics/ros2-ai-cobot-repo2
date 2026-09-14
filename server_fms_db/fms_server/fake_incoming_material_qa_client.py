from datetime import datetime, timezone
from typing import Literal

from fms_server.incoming_material_qa_client import (
    IncomingMaterialQAClient,
    IncomingMaterialQAClientTimeoutError,
    IncomingMaterialQAClientError
)
from shared.schemas.vision import IncomingMaterialQARequest, IncomingMaterialQAResult

class FakeIncomingMaterialQAClient(IncomingMaterialQAClient):
    """
    Fake adapter for testing Incoming Material QA without real UDP/Vision transport.
    Supports deterministic outcomes based on request configuration or internal state.
    """
    def __init__(self):
        # Default is PASS.
        self.next_outcome: Literal["PASS", "FAIL", "NOT_EVALUATED", "ERROR", "TIMEOUT"] = "PASS"
        self.next_failure_type: Literal["MISSING", "WRONG_CLASS", "QUANTITY_MISMATCH", "DEFECT", "MULTIPLE_FAILURE"] | None = None

    def request_inspection(
        self, request: IncomingMaterialQARequest, timeout_sec: float = 10.0
    ) -> IncomingMaterialQAResult:

        if self.next_outcome == "TIMEOUT":
            raise IncomingMaterialQAClientTimeoutError("Fake transport timeout simulated.")

        if self.next_outcome == "ERROR":
            raise IncomingMaterialQAClientError("Fake transport system error simulated.")

        result_status = self.next_outcome
        failure_type = self.next_failure_type if result_status == "FAIL" else None

        detected_qty = request.expected_quantity if result_status == "PASS" else 0
        production_valid = (result_status == "PASS")

        return IncomingMaterialQAResult(
            inspection_request_id=request.inspection_request_id,
            delivery_item_id=request.delivery_item_id,
            inspection_cycle=request.inspection_cycle,
            result=result_status, # type: ignore
            failure_type=failure_type,
            expected_part_code=request.expected_part_code,
            expected_class_name=request.expected_class_name,
            expected_quantity=request.expected_quantity,
            detected_quantity=detected_qty,
            detections=[],
            frame_width=1920,
            frame_height=1080,
            camera_source="GLOBAL_CAMERA",
            frame_seq=1,
            timestamp=datetime.now(timezone.utc),
            model_scope="DB-S4-MOCK",
            model_version="v0.0.1",
            production_valid=production_valid
        )
