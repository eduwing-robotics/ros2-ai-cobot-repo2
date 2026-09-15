"""Map detailed production JobSteps to the verified Robot Cell ExecuteTask contract."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from fms_server.cell_action_transport import (
    CELL_ACTION_CONTRACT_VERSION,
    CellActionTransport,
    CellTaskAcceptedCallback,
    CellTaskCommand,
    CellTaskExecutionResult,
    CellTaskFeedbackCallback,
)
from shared.models.factory import JobStep, ProductionJob

logger = logging.getLogger(__name__)

CELL_TASK_TYPES = frozenset({
    "MATERIAL_FEED", "INSTALL_FLOOR", "INSTALL_INNER_WALL", "INSTALL_FURNITURE",
    "INSTALL_OUTER_WALL", "INSTALL_WINDOW_DOOR", "INSTALL_ROOF",
})
CELL_PART_CLASSES = frozenset({
    # Current Robot Cell coarse manipulation classes.
    "base", "wall_int", "wall_ext", "roof",
    # Legacy classes remain accepted for already-snapshotted historical goals.
    "wall_ext_back", "wall_ext_door", "wall_ext_left", "wall_ext_right",
    "wall_int_house_a", "wall_int_common", "window", "door",
    "base_house_a", "base_house_b",
    "furniture_bath", "furniture_fridge", "furniture_gas_table",
    "furniture_sink", "furniture_toilet", "furniture_washing_machine", "furniture_washstand",
    "roof_01", "roof_02", "wall_int_house_b"
})


class RobotCellActionAdapterError(RuntimeError):
    """Base error for rejected FMS-to-Cell contract mappings."""


class CellTaskMappingError(RobotCellActionAdapterError):
    """A JobStep cannot safely be represented by the agreed Goal contract."""


class MissingOperationCodeError(CellTaskMappingError):
    """A recipe/runtime Step without an operation code must not be dispatched."""


class UnknownOperationCodeError(CellTaskMappingError):
    """An operation code has no approved ExecuteTask task_type mapping."""


class InvalidPartsPayloadError(CellTaskMappingError):
    """The caller did not supply the verified JSON-array parts payload."""


class CellTaskResultContractError(RobotCellActionAdapterError):
    """A successful Robot Cell Result cannot prove all requested slots completed."""


class RequestIdPayloadConflictError(RobotCellActionAdapterError):
    """A process-local req_id was reused for different logical command content."""


class CellDispatchBlockReason(StrEnum):
    """Read-only reasons a snapshot cannot safely construct an Action Goal."""

    MISSING_OPERATION_CODE = "MISSING_OPERATION_CODE"
    MISSING_PRODUCT_CODE = "MISSING_PRODUCT_CODE"
    UNSUPPORTED_OPERATION_CODE = "UNSUPPORTED_OPERATION_CODE"
    PARTS_PAYLOAD_REQUIRED = "PARTS_PAYLOAD_REQUIRED"


@dataclass(frozen=True, slots=True)
class CellPartSpec:
    """One verified item in ExecuteTask Goal.parts_json.

    ``class_name`` is used in Python because ``class`` is reserved; serialization
    always uses the Robot Cell contract key ``class``.
    """

    slot: str
    class_name: str
    part_code: str | None = None
    zone: str | None = None

    @classmethod
    def from_mapping(cls, item: object, *, index: int) -> "CellPartSpec":
        if not isinstance(item, Mapping):
            raise InvalidPartsPayloadError(f"parts_json[{index}] must be a JSON object.")
        slot = cls._required_text(item.get("slot"), field_name=f"parts_json[{index}].slot")
        class_name = cls._required_text(item.get("class"), field_name=f"parts_json[{index}].class")
        if class_name not in CELL_PART_CLASSES:
            raise InvalidPartsPayloadError(
                f"parts_json[{index}].class must be one of {sorted(CELL_PART_CLASSES)}."
            )
        return cls(
            slot=slot,
            class_name=class_name,
            part_code=cls._optional_text(item.get("part_code"), field_name=f"parts_json[{index}].part_code"),
            zone=cls._optional_text(item.get("zone"), field_name=f"parts_json[{index}].zone"),
        )

    @staticmethod
    def _required_text(value: object, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidPartsPayloadError(f"{field_name} is required and must be a non-empty string.")
        return value.strip()

    @staticmethod
    def _optional_text(value: object, *, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise InvalidPartsPayloadError(f"{field_name} must be a non-empty string when supplied.")
        return value.strip()

    def as_contract_dict(self) -> dict[str, str]:
        value = {"slot": self.slot, "class": self.class_name}
        if self.part_code is not None:
            value["part_code"] = self.part_code
        if self.zone is not None:
            value["zone"] = self.zone
        return value


class RobotCellPartClassMapper:
    """Pure, contract-derived part class mapping for detailed install operations.

    This is not a Part master-data mapping: it does not choose a part_code, slot,
    zone, or physical instance.  MATERIAL_FEED intentionally has no single class
    because one delivery batch may contain heterogeneous parts.
    """

    _OPERATION_TO_PART_CLASS = {
        "INSTALL_BASE": "base",
        "INSTALL_TOILET": "furniture_toilet",
        "INSTALL_BASIN": "furniture_washstand",
        "INSTALL_KITCHEN_SINK": "furniture_sink",
        "INSTALL_COOKTOP": "furniture_gas_table",
        "INSTALL_REFRIGERATOR": "furniture_fridge",
        "INSTALL_WASHING_MACHINE": "furniture_washing_machine",
        "INSTALL_BATHTUB": "furniture_bath",
        "INSTALL_COMMON_INNER_WALL": "wall_int",
        "INSTALL_HOUSE_A_INNER_WALL": "wall_int",
        "INSTALL_INNER_WALL": "wall_int",
        "INSTALL_LEFT_OUTER_WALL": "wall_ext",
        "INSTALL_RIGHT_OUTER_WALL": "wall_ext",
        "INSTALL_REAR_OUTER_WALL": "wall_ext",
        "INSTALL_DOOR_OUTER_WALL": "wall_ext",
        "INSTALL_WINDOW_01": "window",
        "INSTALL_WINDOW_02": "window",
        "INSTALL_DOOR": "door",
        "INSTALL_ROOF": "roof",
    }

    @classmethod
    def map_operation_code(cls, operation_code: str | None) -> str:
        normalized = operation_code.strip() if isinstance(operation_code, str) else ""
        if not normalized:
            raise MissingOperationCodeError("JobStep.operation_code is required before resolving Cell part class.")
        try:
            return cls._OPERATION_TO_PART_CLASS[normalized]
        except KeyError as exc:
            raise UnknownOperationCodeError(
                f"JobStep.operation_code={normalized!r} has no approved Cell part-class mapping."
            ) from exc


class RobotCellTaskTypeMapper:
    """Pure approved mapping from detailed server operations to Cell task types."""

    _OPERATION_TO_TASK_TYPE = {
        "MATERIAL_FEED": "MATERIAL_FEED",
        "INSTALL_BASE": "INSTALL_FLOOR",
        "INSTALL_TOILET": "INSTALL_FURNITURE",
        "INSTALL_BASIN": "INSTALL_FURNITURE",
        "INSTALL_KITCHEN_SINK": "INSTALL_FURNITURE",
        "INSTALL_COOKTOP": "INSTALL_FURNITURE",
        "INSTALL_REFRIGERATOR": "INSTALL_FURNITURE",
        "INSTALL_WASHING_MACHINE": "INSTALL_FURNITURE",
        "INSTALL_BATHTUB": "INSTALL_FURNITURE",
        "INSTALL_COMMON_INNER_WALL": "INSTALL_INNER_WALL",
        "INSTALL_HOUSE_A_INNER_WALL": "INSTALL_INNER_WALL",
        "INSTALL_INNER_WALL": "INSTALL_INNER_WALL",
        "INSTALL_LEFT_OUTER_WALL": "INSTALL_OUTER_WALL",
        "INSTALL_RIGHT_OUTER_WALL": "INSTALL_OUTER_WALL",
        "INSTALL_REAR_OUTER_WALL": "INSTALL_OUTER_WALL",
        "INSTALL_DOOR_OUTER_WALL": "INSTALL_OUTER_WALL",
        "INSTALL_WINDOW_01": "INSTALL_WINDOW_DOOR",
        "INSTALL_WINDOW_02": "INSTALL_WINDOW_DOOR",
        "INSTALL_DOOR": "INSTALL_WINDOW_DOOR",
        "INSTALL_ROOF": "INSTALL_ROOF",
    }

    @classmethod
    def map_operation_code(cls, operation_code: str | None) -> str:
        normalized = operation_code.strip() if isinstance(operation_code, str) else ""
        if not normalized:
            raise MissingOperationCodeError("JobStep.operation_code is required before Robot Cell dispatch.")
        try:
            return cls._OPERATION_TO_TASK_TYPE[normalized]
        except KeyError as exc:
            raise UnknownOperationCodeError(
                f"JobStep.operation_code={normalized!r} has no approved ExecuteTask task_type mapping."
            ) from exc


@dataclass(frozen=True, slots=True)
class CellDispatchability:
    """Pure contract assessment; it never allocates req_id or contacts transport."""

    dispatchable: bool
    block_reason: CellDispatchBlockReason | None = None


class RobotCellActionAdapter:
    """DB-read-only bridge from detailed JobSteps to Robot Cell Actions.

    The caller owns ``req_id``. Job and Step identifiers are only opaque Goal
    strings; the request fingerprint intentionally keys idempotency on ``req_id``.
    """

    def __init__(self, transport: CellActionTransport, *, contract_version: str = CELL_ACTION_CONTRACT_VERSION) -> None:
        normalized_version = contract_version.strip()
        if not normalized_version:
            raise ValueError("contract_version must be non-empty.")
        self._transport = transport
        self._contract_version = normalized_version
        self._request_fingerprints: dict[str, tuple[object, ...]] = {}

    @staticmethod
    def assess_dispatchability(*, job: ProductionJob, step: JobStep) -> CellDispatchability:
        """Pure snapshot-only validation; monitoring has no caller payload to send."""

        try:
            RobotCellTaskTypeMapper.map_operation_code(step.operation_code)
        except MissingOperationCodeError:
            return CellDispatchability(False, CellDispatchBlockReason.MISSING_OPERATION_CODE)
        except UnknownOperationCodeError:
            return CellDispatchability(False, CellDispatchBlockReason.UNSUPPORTED_OPERATION_CODE)
        product_code = job.product.product_code.strip() if job.product is not None else ""
        if not product_code:
            return CellDispatchability(False, CellDispatchBlockReason.MISSING_PRODUCT_CODE)
        # No production BOM/slot materializer exists yet, including for roofs.
        return CellDispatchability(False, CellDispatchBlockReason.PARTS_PAYLOAD_REQUIRED)

    @staticmethod
    def serialize_parts_payload(parts: Sequence[CellPartSpec]) -> str:
        """Serialize structured, already-resolved Cell parts at the adapter boundary."""
        return json.dumps([part.as_contract_dict() for part in parts], separators=(",", ":"))

    @staticmethod
    def requested_slots_from_parts_json(parts_json: str) -> tuple[str, ...]:
        """Return verified Goal slot identities before an Action Attempt is created."""
        _, parts = RobotCellActionAdapter._validate_parts_json(parts_json)
        return tuple(part.slot for part in parts)

    def validate_successful_result(
        self,
        *,
        command: CellTaskCommand,
        result: CellTaskExecutionResult,
    ) -> None:
        """Require a SUCCEEDED Result to account for the exact Goal slot multiset.

        ``completed_json`` order is telemetry, not an execution identity. A
        multiset comparison catches missing, extra, and duplicate completion
        records without imposing an artificial Robot Cell completion order.
        """
        if result.succeeded is not True:
            return
        requested_slots = self.requested_slots_from_parts_json(command.parts_json)
        if Counter(requested_slots) != Counter(result.completed_slots):
            raise CellTaskResultContractError(
                "Robot Cell SUCCEEDED Result.completed_json does not match requested Goal slots "
                f"(requested={requested_slots!r}, completed={result.completed_slots!r})."
            )

    def build_task_command(
        self,
        *,
        job: ProductionJob,
        task_type: str,
        opaque_step_id: str,
        req_id: str,
        parts_json: str,
    ) -> CellTaskCommand:
        """Build a verified non-JobStep Goal, currently MATERIAL_FEED only."""
        normalized_task_type = self._require_text(task_type, field_name="task_type")
        if normalized_task_type not in CELL_TASK_TYPES:
            raise CellTaskMappingError(f"task_type={normalized_task_type!r} is not an approved ExecuteTask task type.")
        normalized_step_id = self._require_text(opaque_step_id, field_name="opaque_step_id")
        if job.job_id is None or job.job_id <= 0 or job.product is None:
            raise CellTaskMappingError("Persisted ProductionJob with loaded product is required for Cell dispatch.")
        product_code = self._require_text(job.product.product_code, field_name="Product.product_code")
        normalized_parts_json, _ = self._validate_parts_json(parts_json)
        return CellTaskCommand(
            ver=self._contract_version,
            req_id=self._require_text(req_id, field_name="req_id"),
            job_id=str(job.job_id),
            step_id=normalized_step_id,
            task_type=normalized_task_type,
            product=product_code,
            parts_json=normalized_parts_json,
        )

    def build_command(
        self,
        *,
        job: ProductionJob,
        step: JobStep,
        req_id: str,
        parts_json: str,
    ) -> CellTaskCommand:
        """Build one exact Goal without sending it or mutating database state."""

        normalized_req_id = self._require_text(req_id, field_name="req_id")
        task_type = RobotCellTaskTypeMapper.map_operation_code(step.operation_code)
        if step.job_id != job.job_id:
            raise CellTaskMappingError(
                f"JobStep job_id={step.job_id} does not belong to ProductionJob job_id={job.job_id}."
            )
        if step.job_step_id is None or step.job_step_id <= 0:
            raise CellTaskMappingError("JobStep.job_step_id must be a positive persisted identifier.")
        if job.job_id is None or job.job_id <= 0:
            raise CellTaskMappingError("ProductionJob.job_id must be a positive persisted identifier.")
        if job.product is None:
            raise CellTaskMappingError("ProductionJob.product must be loaded to map Goal.product.")
        product_code = self._require_text(job.product.product_code, field_name="Product.product_code")
        normalized_parts_json, parts = self._validate_parts_json(parts_json)
        self._validate_roof_parts(job=job, task_type=task_type, parts=parts)
        return CellTaskCommand(
            ver=self._contract_version,
            req_id=normalized_req_id,
            job_id=str(job.job_id),
            step_id=str(step.job_step_id),
            task_type=task_type,
            product=product_code,
            parts_json=normalized_parts_json,
        )

    def execute(
        self,
        command: CellTaskCommand,
        *,
        goal_accepted_callback: CellTaskAcceptedCallback | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> CellTaskExecutionResult:
        fingerprint = (
            command.ver, command.job_id, command.step_id, command.task_type, command.product, command.parts_json,
        )
        previous = self._request_fingerprints.setdefault(command.req_id, fingerprint)
        if previous != fingerprint:
            raise RequestIdPayloadConflictError(
                f"req_id={command.req_id!r} was already used for different command content in this process."
            )
        logger.info("Cell goal sending req_id=%s job_id=%s step_id=%s task_type=%s", command.req_id, command.job_id, command.step_id, command.task_type)
        result = self._transport.execute(command, goal_accepted_callback=goal_accepted_callback, feedback_callback=feedback_callback)
        logger.info("Cell goal result req_id=%s job_id=%s step_id=%s task_type=%s outcome=%s", command.req_id, command.job_id, command.step_id, command.task_type, result.outcome)
        return result

    def dispatch_step(
        self,
        *,
        job: ProductionJob,
        step: JobStep,
        req_id: str,
        parts_json: str,
        goal_accepted_callback: CellTaskAcceptedCallback | None = None,
        feedback_callback: CellTaskFeedbackCallback | None = None,
    ) -> CellTaskExecutionResult:
        return self.execute(
            self.build_command(job=job, step=step, req_id=req_id, parts_json=parts_json),
            goal_accepted_callback=goal_accepted_callback,
            feedback_callback=feedback_callback,
        )

    @staticmethod
    def _require_text(value: str | None, *, field_name: str) -> str:
        if value is None or not value.strip():
            raise CellTaskMappingError(f"{field_name} must be non-empty.")
        return value.strip()

    @staticmethod
    def _validate_parts_json(parts_json: str) -> tuple[str, tuple[CellPartSpec, ...]]:
        if not isinstance(parts_json, str) or not parts_json.strip():
            raise InvalidPartsPayloadError("parts_json must be a non-empty JSON array supplied by the caller.")
        try:
            parsed = json.loads(parts_json)
        except json.JSONDecodeError as exc:
            raise InvalidPartsPayloadError("parts_json must be valid JSON.") from exc
        if not isinstance(parsed, list):
            raise InvalidPartsPayloadError("parts_json must encode a JSON array, not an object.")
        parts = tuple(CellPartSpec.from_mapping(item, index=index) for index, item in enumerate(parsed))
        if not parts:
            raise InvalidPartsPayloadError("parts_json must contain at least one requested part.")
        # Keep caller formatting/content after validation: it is the exact Goal payload.
        return parts_json, parts

    @staticmethod
    def _validate_roof_parts(*, job: ProductionJob, task_type: str, parts: Sequence[CellPartSpec]) -> None:
        if task_type != "INSTALL_ROOF":
            return
        expected_option = job.roof_option_code.value if job.roof_option_code is not None else None
        if expected_option is None:
            raise CellTaskMappingError("INSTALL_ROOF requires ProductionJob.roof_option_code.")
        # ``roof_option_code`` selects the gated RecipeStage.  The selected
        # Part Master owns the physical part identifier, while Robot Cell gets
        # the coarse manipulation class.  Legacy snapshots remain accepted.
        has_current_roof = any(
            part.class_name == "roof" and isinstance(part.part_code, str) and part.part_code.strip()
            for part in parts
        )
        has_legacy_roof = any(
            part.class_name in ("roof_01", "roof_02") and part.part_code == expected_option
            for part in parts
        )
        if not (has_current_roof or has_legacy_roof):
            raise InvalidPartsPayloadError(
                "INSTALL_ROOF parts_json must include a coarse class='roof' Part Master item "
                "or a compatible legacy roof option item."
            )
