from __future__ import annotations

import builtins
import json

import pytest

from fms_server.cell_action_transport import (
    CELL_ACTION_CONTRACT_VERSION,
    CellTaskExecutionResult,
    CellTaskFeedbackValidationError,
    CellTaskOutcome,
    CellTaskRawFeedback,
    CellTaskRawResult,
    CellTaskResultNormalizationError,
)
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.robot_cell_action_adapter import (
    CELL_PART_CLASSES,
    CELL_TASK_TYPES,
    CellTaskMappingError,
    CellTaskResultContractError,
    InvalidPartsPayloadError,
    MissingOperationCodeError,
    RequestIdPayloadConflictError,
    RobotCellActionAdapter,
    RobotCellTaskTypeMapper,
    UnknownOperationCodeError,
)

def test_structural_parts_use_current_coarse_cell_classes_and_legacy_payloads_remain_readable() -> None:
    for class_name in ("base", "wall_int", "wall_ext", "roof"):
        assert class_name in CELL_PART_CLASSES
    # Historical persisted payloads remain valid; new payload builders do not emit them.
    for class_name in ("wall_ext_back", "base_house_a", "roof_02", "wall_int_house_b"):
        assert class_name in CELL_PART_CLASSES

from fms_server.ros2_cell_action_transport import CellActionContractUnavailableError, Ros2CellActionTransport
from shared.models.factory import JobStatus, JobStep, Product, ProductionJob, RoofOptionCode, StepStatus


def parts(*, slot: str = "SYNTHETIC_SLOT", class_name: str = "base_house_a", part_code: str | None = None, zone: str | None = None) -> str:
    item = {"slot": slot, "class": class_name}
    if part_code is not None:
        item["part_code"] = part_code
    if zone is not None:
        item["zone"] = zone
    return json.dumps([item], separators=(",", ":"))


def _job_and_step(*, operation_code: str | None = "INSTALL_BASE", roof_option_code: RoofOptionCode | None = None) -> tuple[ProductionJob, JobStep]:
    product = Product(product_code="HOUSE_A", product_name="A형 주택")
    job = ProductionJob(job_id=101, job_code="CELL-TEST-101", product_code="HOUSE_A", product=product, roof_option_code=roof_option_code, status=JobStatus.RUNNING)
    step = JobStep(job_step_id=501, job_id=101, step_order=1, operation_code=operation_code, display_name="테스트 작업", status=StepStatus.PENDING)
    if roof_option_code is not None:
        pass # removed in S1
    return job, step


def _adapter(*results: CellTaskRawResult | CellTaskExecutionResult) -> tuple[RobotCellActionAdapter, FakeCellActionTransport]:
    transport = FakeCellActionTransport([FakeCellActionExchange(result) for result in results])
    return RobotCellActionAdapter(transport), transport


@pytest.mark.parametrize(
    ("operation_code", "task_type"),
    [
        ("INSTALL_BASE", "INSTALL_FLOOR"),
        ("INSTALL_TOILET", "INSTALL_FURNITURE"), ("INSTALL_BASIN", "INSTALL_FURNITURE"),
        ("INSTALL_KITCHEN_SINK", "INSTALL_FURNITURE"), ("INSTALL_COOKTOP", "INSTALL_FURNITURE"),
        ("INSTALL_REFRIGERATOR", "INSTALL_FURNITURE"), ("INSTALL_WASHING_MACHINE", "INSTALL_FURNITURE"),
        ("INSTALL_BATHTUB", "INSTALL_FURNITURE"),
        ("INSTALL_COMMON_INNER_WALL", "INSTALL_INNER_WALL"), ("INSTALL_HOUSE_A_INNER_WALL", "INSTALL_INNER_WALL"), ("INSTALL_INNER_WALL", "INSTALL_INNER_WALL"),
        ("INSTALL_LEFT_OUTER_WALL", "INSTALL_OUTER_WALL"), ("INSTALL_RIGHT_OUTER_WALL", "INSTALL_OUTER_WALL"),
        ("INSTALL_REAR_OUTER_WALL", "INSTALL_OUTER_WALL"), ("INSTALL_DOOR_OUTER_WALL", "INSTALL_OUTER_WALL"),
        ("INSTALL_WINDOW_01", "INSTALL_WINDOW_DOOR"), ("INSTALL_WINDOW_02", "INSTALL_WINDOW_DOOR"),
        ("INSTALL_DOOR", "INSTALL_WINDOW_DOOR"), ("INSTALL_ROOF", "INSTALL_ROOF"), ("MATERIAL_FEED", "MATERIAL_FEED"),
    ],
)
def test_approved_recipe_operations_map_to_verified_cell_task_types(operation_code: str, task_type: str) -> None:
    assert RobotCellTaskTypeMapper.map_operation_code(operation_code) == task_type


def test_unknown_or_missing_operation_is_an_explicit_mapping_error() -> None:
    with pytest.raises(MissingOperationCodeError):
        RobotCellTaskTypeMapper.map_operation_code(None)
    with pytest.raises(UnknownOperationCodeError):
        RobotCellTaskTypeMapper.map_operation_code("INSTALL_UNAPPROVED")


def test_recipe_step_maps_exact_goal_without_mutating_objects() -> None:
    job, step = _job_and_step(operation_code="INSTALL_LEFT_OUTER_WALL")
    adapter, transport = _adapter(CellTaskRawResult(status="SUCCEEDED", detail="done", completed_json='["S1"]'))

    result = adapter.dispatch_step(job=job, step=step, req_id="req-cell-001", parts_json=parts(class_name="wall_ext_left"))

    command = transport.commands[0]
    assert command.ver == "0.2.1"
    assert command.ver == CELL_ACTION_CONTRACT_VERSION
    assert command.req_id == "req-cell-001"
    assert command.job_id == "101" and command.step_id == "501"
    assert command.task_type == "INSTALL_OUTER_WALL"
    assert command.product == "HOUSE_A"
    assert json.loads(command.parts_json)[0]["class"] == "wall_ext_left"
    assert result.outcome is CellTaskOutcome.SUCCESS
    assert result.detail == "done" and result.completed_slots == ("S1",)
    assert job.status is JobStatus.RUNNING and step.status is StepStatus.PENDING


def test_parts_json_requires_non_empty_verified_array_items_and_allows_optional_fields() -> None:
    job, step = _job_and_step()
    adapter, _ = _adapter(CellTaskExecutionResult.success())
    valid = adapter.build_command(job=job, step=step, req_id="valid", parts_json=parts(class_name="base_house_a"))
    assert json.loads(valid.parts_json) == [{"slot": "SYNTHETIC_SLOT", "class": "base_house_a"}]
    for invalid in ('{}', '{"parts":[]}', 'not json', '[]', '[{"class":"base_house_a"}]', '[{"slot":"A"}]', '[{"slot":"A","class":"bad"}]'):
        with pytest.raises(InvalidPartsPayloadError):
            adapter.build_command(job=job, step=step, req_id=f"bad-{invalid}", parts_json=invalid)
    optional = adapter.build_command(job=job, step=step, req_id="optional", parts_json=parts(class_name="base_house_a", part_code="OPTIONAL", zone="ZONE"))
    assert json.loads(optional.parts_json)[0]["part_code"] == "OPTIONAL"


def test_empty_parts_json_is_rejected_before_the_transport_is_called() -> None:
    job, step = _job_and_step()
    adapter, transport = _adapter(CellTaskExecutionResult.success())

    with pytest.raises(InvalidPartsPayloadError):
        adapter.dispatch_step(job=job, step=step, req_id="empty-goal", parts_json="[]")

    assert transport.commands == []


@pytest.mark.parametrize(
    ("requested", "completed", "is_valid"),
    [
        (("A",), ("A",), True),
        (("A", "B"), ("A", "B"), True),
        (("A", "B"), ("B", "A"), True),
        (("A", "B"), ("A",), False),
        (("A",), ("A", "X"), False),
        (("A", "B"), ("A", "A"), False),
    ],
)
def test_succeeded_result_requires_the_exact_requested_slot_multiset(
    requested: tuple[str, ...], completed: tuple[str, ...], is_valid: bool
) -> None:
    job, step = _job_and_step()
    adapter, _ = _adapter(CellTaskExecutionResult.success())
    payload = json.dumps([{"slot": slot, "class": "base_house_a"} for slot in requested])
    command = adapter.build_command(job=job, step=step, req_id="completed-slots", parts_json=payload)
    result = CellTaskExecutionResult.success(completed_slots=completed)

    if is_valid:
        adapter.validate_successful_result(command=command, result=result)
    else:
        with pytest.raises(CellTaskResultContractError):
            adapter.validate_successful_result(command=command, result=result)


def test_roof_uses_verified_parts_transport_without_fabricating_a_slot() -> None:
    job, step = _job_and_step(operation_code="INSTALL_ROOF", roof_option_code=RoofOptionCode.ROOF_02)
    adapter, transport = _adapter(CellTaskRawResult(status="SUCCEEDED", completed_json='["SYNTHETIC_RF"]'))
    result = adapter.dispatch_step(job=job, step=step, req_id="roof", parts_json=parts(slot="SYNTHETIC_RF", class_name="roof_01", part_code="ROOF_02"))
    assert transport.commands[0].task_type == "INSTALL_ROOF"
    assert json.loads(transport.commands[0].parts_json)[0]["part_code"] == "ROOF_02"
    assert result.outcome is CellTaskOutcome.SUCCESS
    with pytest.raises(InvalidPartsPayloadError):
        adapter.build_command(job=job, step=step, req_id="roof-missing-option", parts_json=parts(slot="S", class_name="roof_01"))


def test_req_id_is_the_only_in_process_idempotency_key() -> None:
    job, step = _job_and_step()
    adapter, transport = _adapter(CellTaskExecutionResult.success(), CellTaskExecutionResult.success(), CellTaskExecutionResult.success())
    first = adapter.build_command(job=job, step=step, req_id="req-stable", parts_json=parts())
    same_step_new_req = adapter.build_command(job=job, step=step, req_id="req-retry", parts_json=parts())
    adapter.execute(first)
    adapter.execute(same_step_new_req)
    with pytest.raises(RequestIdPayloadConflictError):
        adapter.execute(adapter.build_command(job=job, step=step, req_id="req-stable", parts_json=parts(slot="DIFFERENT")))
    assert [command.step_id for command in transport.commands] == ["501", "501"]
    assert [command.req_id for command in transport.commands] == ["req-stable", "req-retry"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (CellTaskRawResult(status="SUCCEEDED", error_code="", detail="ok", completed_json='["A"]'), CellTaskOutcome.SUCCESS),
        (CellTaskRawResult(status="FAILED", error_code="E503", detail="fault", completed_json='["A"]'), CellTaskOutcome.CELL_FAILED),
        (CellTaskRawResult(status="CANCELED", detail="operator canceled", completed_json='[]'), CellTaskOutcome.CELL_CANCELED),
    ],
)
def test_raw_result_normalization_preserves_actual_contract_fields(raw: CellTaskRawResult, expected: CellTaskOutcome) -> None:
    normalized = raw.normalize()
    assert normalized.outcome is expected
    assert normalized.detail == raw.detail
    assert normalized.completed_slots == tuple(json.loads(raw.completed_json))
    assert normalized.error_code is None if raw.error_code == "" else normalized.error_code == raw.error_code


def test_malformed_completed_json_is_rejected_not_silently_erased() -> None:
    with pytest.raises(CellTaskResultNormalizationError):
        CellTaskRawResult(status="SUCCEEDED", completed_json="{").normalize()


def test_verified_feedback_is_preserved_and_progress_is_not_completion() -> None:
    job, step = _job_and_step()
    transport = FakeCellActionTransport([FakeCellActionExchange(
        CellTaskRawResult(status="SUCCEEDED", completed_json='["S"]'),
        feedback=(
            CellTaskRawFeedback("OBSERVE", 0, 1, 0.0, "fr5"),
            CellTaskRawFeedback("PLACE_APPROACH", 0, 1, 1.0, "zk2"),
        ),
    )])
    adapter = RobotCellActionAdapter(transport)
    feedback = []
    result = adapter.dispatch_step(job=job, step=step, req_id="feedback", parts_json=parts(), feedback_callback=feedback.append)
    assert [(item.job_id, item.step_id, item.phase, item.current_item, item.total_items, item.progress, item.robot) for item in feedback] == [
        ("101", "501", "OBSERVE", 0, 1, 0.0, "fr5"), ("101", "501", "PLACE_APPROACH", 0, 1, 1.0, "zk2"),
    ]
    assert result.outcome is CellTaskOutcome.SUCCESS
    assert step.status is StepStatus.PENDING
    with pytest.raises(CellTaskFeedbackValidationError):
        CellTaskRawFeedback("OBSERVE", 0, 1, 1.1, "fr5").validate()


def test_deferred_ros2_helpers_use_only_verified_generated_fields() -> None:
    class Goal:
        pass

    class Result:
        status = "FAILED"
        error_code = "E503"
        detail = "cell detail"
        completed_json = '["S1"]'

    class Feedback:
        phase = "PICK"
        current_item = 0
        total_items = 1
        progress = 0.5
        robot = "zk2"

    from fms_server.cell_action_transport import CellTaskCommand

    command = CellTaskCommand("0.2.1", "req", "JOB-19", "STEP-456", "INSTALL_FURNITURE", "HOUSE_A", parts(class_name="furniture_bath"))
    goal = Ros2CellActionTransport._populate_goal(Goal(), command)
    assert (goal.ver, goal.req_id, goal.job_id, goal.step_id, goal.task_type, goal.product, goal.parts_json) == (
        "0.2.1", "req", "JOB-19", "STEP-456", "INSTALL_FURNITURE", "HOUSE_A", command.parts_json,
    )
    normalized = Ros2CellActionTransport._normalize_generated_result(Result())
    feedback = Ros2CellActionTransport._normalize_generated_feedback(command, Feedback())
    assert normalized.outcome is CellTaskOutcome.CELL_FAILED and normalized.error_code == "E503"
    assert (feedback.phase, feedback.current_item, feedback.total_items, feedback.progress, feedback.robot) == ("PICK", 0, 1, 0.5, "zk2")


def test_ros2_transport_is_explicitly_unavailable_without_generated_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def missing_generated_interface(name, *args, **kwargs):
        if name == "cell_interfaces.action":
            raise ImportError("synthetic missing generated interface")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_generated_interface)
    with pytest.raises(CellActionContractUnavailableError, match="cell_interfaces.action.ExecuteTask"):
        Ros2CellActionTransport()
