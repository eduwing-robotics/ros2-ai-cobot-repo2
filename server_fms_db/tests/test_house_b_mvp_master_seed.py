from __future__ import annotations

from datetime import datetime, timezone
import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_material_qa_dispatch_coordinator import (
    IncomingMaterialQADispatchAction,
    IncomingMaterialQADispatchCoordinator,
)
from fms_server.transport_location_resolver import resolve_policy_transport_locations
from scripts.seed_house_b_mvp_master import (
    HOUSE_B_PARTS,
    HOUSE_B_PRODUCT_CODE,
    HOUSE_B_SLOTS,
    HOUSE_B_STAGES,
    seed_house_b_mvp_master,
)
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialInspection,
    Part,
    ProductionInspection,
    ProductionInspectionStatus,
    RoofOptionCode,
    SupplyMode,
)
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService
from shared.services.transport_eligibility_service import TransportEligibilityReason, TransportEligibilityService
from shared.services.production_cell_payload_builder import ProductionCellPayloadBuilder
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.schemas.vision import IncomingMaterialQAResult, QAWireDetection
from tests.recipe_test_support import seed_inventory_for_recipe


EXPECTED_VISION_CLASSES = {
    "BASE-HOUSE-B-01": "base_house_b",
    "WALL-INT-HOUSE-B-01": "wall_int_house_b",
    "WALL-EXT-BACK-WINDOW-01": "wall_ext_back_window",
    "WALL-EXT-DOOR-01": "wall_ext_door",
    "WALL-EXT-LEFT-WINDOW-01": "wall_ext_left_window",
    "WALL-EXT-RIGHT-01": "wall_ext_right",
    "ROOF-ZIP-01": "roof_zip",
}


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        yield db
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(session: Session) -> AssemblyRecipe:
    recipe = seed_house_b_mvp_master(session)
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return recipe


def _create_job(session: Session):
    _seed(session)
    return ProductionOrchestrationService(session).create_job(
        product_code=HOUSE_B_PRODUCT_CODE,
        job_code="HOUSE-B-MVP-TEST-001",
        roof_option_code=RoofOptionCode.ROOF_02,
    )


def test_house_b_master_seed_is_idempotent_and_preserves_exact_parts_slots_recipe(session: Session) -> None:
    first = _seed(session)
    second = seed_house_b_mvp_master(session)
    session.commit()
    assert first.recipe_id == second.recipe_id
    assert session.scalar(select(func.count()).select_from(Part).where(Part.part_code.in_(EXPECTED_VISION_CLASSES))) == 7
    assert {
        code: vision_class
        for code, vision_class in session.execute(
            select(Part.part_code, Part.vision_class).where(Part.part_code.in_(EXPECTED_VISION_CLASSES))
        )
    } == EXPECTED_VISION_CLASSES
    assert {row[0] for row in session.execute(select(__import__("shared.models.factory", fromlist=["InstallationSlot"]).InstallationSlot.slot_code))} == {
        row[0] for row in HOUSE_B_SLOTS
    }
    stages = list(session.scalars(
        select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == first.recipe_id)
        .order_by(AssemblyRecipeStage.stage_order)
    ))
    assert len(stages) == 7
    assert [stage.operation_code for stage in stages] == [row[1] for row in HOUSE_B_STAGES]
    assert [stage.stage_order for stage in stages] == list(range(1, 8))
    assert [stage.operation_code for stage in stages[1:5]] == [
        "INSTALL_DOOR_OUTER_WALL",
        "INSTALL_LEFT_OUTER_WALL",
        "INSTALL_REAR_OUTER_WALL",
        "INSTALL_RIGHT_OUTER_WALL",
    ]
    assert stages[5].operation_code == "INSTALL_INNER_WALL"
    roof = stages[-1]
    assert roof.execution_gate == "PRE_ROOF_PASS"
    assert roof.option_code == RoofOptionCode.ROOF_02.value
    assert all(stage.operation_code not in {"MATERIAL_FEED", "INSTALL_FURNITURE", "INSTALL_WINDOW_DOOR"} for stage in stages)


def test_house_b_job_snapshot_groups_transported_walls_and_uses_logical_drop_routes(session: Session) -> None:
    job = _create_job(session)
    steps = list(session.scalars(
        select(JobStep).where(JobStep.job_id == job.job_id).order_by(JobStep.step_order)
    ))
    assert len(steps) == 6
    assert [step.part_code for step in steps] == [row[3] for row in HOUSE_B_STAGES[:6]]
    assert not any(step.operation_code == "INSTALL_ROOF" for step in steps)

    deliveries = list(session.scalars(
        select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id)
        .order_by(JobMaterialDelivery.supply_group_code)
    ))
    by_group = {delivery.supply_group_code: delivery for delivery in deliveries}
    assert set(by_group) == {"BASE", "INNER_WALL", "OUTER_WALLS", "ROOF"}
    assert by_group["BASE"].supply_mode is SupplyMode.MANUAL
    assert by_group["INNER_WALL"].supply_mode is SupplyMode.TRANSPORTED
    assert by_group["OUTER_WALLS"].supply_mode is SupplyMode.TRANSPORTED
    assert by_group["ROOF"].supply_mode is SupplyMode.MANUAL
    assert by_group["BASE"].batch_order < by_group["OUTER_WALLS"].batch_order < by_group["INNER_WALL"].batch_order < by_group["ROOF"].batch_order

    inner_items = list(session.scalars(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == by_group["INNER_WALL"].job_delivery_id
    )))
    outer_items = list(session.scalars(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == by_group["OUTER_WALLS"].job_delivery_id
    )))
    assert [(item.part_code, item.quantity) for item in inner_items] == [("WALL-INT-HOUSE-B-01", 1)]
    assert {(item.part_code, item.quantity) for item in outer_items} == {
        ("WALL-EXT-BACK-WINDOW-01", 1), ("WALL-EXT-DOOR-01", 1),
        ("WALL-EXT-LEFT-WINDOW-01", 1), ("WALL-EXT-RIGHT-01", 1),
    }
    roof_items = list(session.scalars(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == by_group["ROOF"].job_delivery_id
    )))
    assert [(item.part_code, item.quantity, item.job_step_id) for item in roof_items] == [
        ("ROOF-ZIP-01", 1, None)
    ]
    assert len(inner_items) + len(outer_items) + len(roof_items) + 1 == 7
    assert resolve_policy_transport_locations(by_group["INNER_WALL"]).pickup_code == "RACK2"
    assert resolve_policy_transport_locations(by_group["INNER_WALL"]).dropoff_code == "DROP"
    assert resolve_policy_transport_locations(by_group["OUTER_WALLS"]).pickup_code == "RACK1"
    assert resolve_policy_transport_locations(by_group["OUTER_WALLS"]).dropoff_code == "DROP"


def test_house_b_cell_payload_uses_robot_coarse_classes_not_vision_classes(session: Session) -> None:
    job = _create_job(session)
    steps = {step.operation_code: step for step in session.scalars(select(JobStep).where(JobStep.job_id == job.job_id))}
    builder = ProductionCellPayloadBuilder(session)
    inner = builder.build_for_job_step(job_id=job.job_id, job_step_id=steps["INSTALL_INNER_WALL"].job_step_id).parts[0]
    rear = builder.build_for_job_step(job_id=job.job_id, job_step_id=steps["INSTALL_REAR_OUTER_WALL"].job_step_id).parts[0]
    assert inner.as_contract_dict() == {
        "slot": "red_in", "class": "wall_int",
        "part_code": "WALL-INT-HOUSE-B-01", "zone": "WALL_INT_PALLET",
    }
    assert rear.as_contract_dict() == {
        "slot": "red", "class": "wall_ext",
        "part_code": "WALL-EXT-BACK-WINDOW-01", "zone": "WALL_EXT_PALLET",
    }


def test_house_b_all_material_items_support_preproduction_qa_without_auto_start(session: Session) -> None:
    job = _create_job(session)
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 0
    assert not session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF"
    ))
    deliveries = list(session.scalars(select(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id,
    )))
    items = list(session.scalars(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id.in_([delivery.job_delivery_id for delivery in deliveries])
    )))
    assert len(items) == 7
    roof_item = next(item for item in items if item.part_code == "ROOF-ZIP-01")
    assert roof_item.job_step_id is None
    # The same explicit operator QA coordinator must accept a deferred item;
    # no JobStep is available yet and no network send occurs in prepare.
    roof_decision = IncomingMaterialQADispatchCoordinator(session).prepare_inspection(
        delivery_item_id=roof_item.delivery_item_id
    )
    assert roof_decision.action is IncomingMaterialQADispatchAction.CREATE_NEW
    assert roof_decision.should_send is True
    service = MaterialInspectionService()
    for item in items:
        if item.delivery_item_id != roof_item.delivery_item_id:
            service.request_inspection(session, item.delivery_item_id)
    inspections = list(session.scalars(select(MaterialInspection)))
    assert {(inspection.expected_part_code, inspection.expected_class_name) for inspection in inspections} == set(
        EXPECTED_VISION_CLASSES.items()
    )

def test_house_b_runtime_roof_binds_existing_preproduction_qa_item_and_preserves_pass(session: Session) -> None:
    job = _create_job(session)
    roof_delivery = session.scalar(select(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id,
        JobMaterialDelivery.supply_group_code == "ROOF",
    ))
    assert roof_delivery is not None and roof_delivery.supply_mode is SupplyMode.MANUAL
    roof_item = session.scalar(select(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == roof_delivery.job_delivery_id
    ))
    assert roof_item is not None and roof_item.job_step_id is None

    inspection_service = MaterialInspectionService()
    request = inspection_service.request_inspection(session, roof_item.delivery_item_id)
    stored = inspection_service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=request.inspection_request_id,
        delivery_item_id=request.delivery_item_id,
        inspection_cycle=request.inspection_cycle,
        status="COMPLETED",
        result="PASS",
        failure_type=None,
        expected_part_code="ROOF-ZIP-01",
        expected_class_name="roof_zip",
        expected_quantity=1,
        detected_quantity=1,
        detections=[QAWireDetection(
            class_name="roof_zip", class_id=1, confidence=0.99,
            bbox_xyxy=[1.0, 1.0, 2.0, 2.0],
        )],
        frame_width=640,
        frame_height=480,
        camera_source="GLOBAL_CAMERA",
        frame_seq=1,
        timestamp=datetime.now(timezone.utc),
        model_scope="house_b",
        model_version="test",
        production_valid=True,
    ))
    session.commit()
    inspection_id = stored.inspection_id
    request_id = stored.inspection_request_id
    cycle = stored.inspection_cycle

    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    for _ in range(6):
        step = orchestration.get_next_step(job.job_id)
        assert step is not None
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)
    inspection = session.scalar(select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id))
    assert inspection is not None and inspection.status is ProductionInspectionStatus.PENDING
    lifecycle = ProductionCompletionService(session)
    lifecycle.start_pre_roof_inspection(production_job_id=job.job_id)
    roof = lifecycle.pass_pre_roof_inspection(production_job_id=job.job_id)
    assert (roof.part_code, roof.vision_class, roof.slot_code, roof.pick_zone) == (
        "ROOF-ZIP-01", "roof_zip", "HOUSE_B_ROOF_01", None
    )
    session.refresh(roof_item)
    assert roof_item.job_step_id == roof.job_step_id
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery).where(
        JobMaterialDelivery.production_job_id == job.job_id,
        JobMaterialDelivery.supply_group_code == "ROOF",
    )) == 1
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem).where(
        JobMaterialDeliveryItem.job_delivery_id == roof_delivery.job_delivery_id,
    )) == 1
    preserved = session.get(MaterialInspection, inspection_id)
    assert preserved is not None
    assert (preserved.inspection_request_id, preserved.inspection_cycle, preserved.result, preserved.production_valid) == (
        request_id, cycle, "PASS", True
    )
    payload = ProductionCellPayloadBuilder(session).build_for_job_step(
        job_id=job.job_id, job_step_id=roof.job_step_id
    )
    assert payload.parts[0].as_contract_dict() == {
        "slot": "HOUSE_B_ROOF_01", "class": "roof", "part_code": "ROOF-ZIP-01"
    }




def _pass_item(session: Session, item: JobMaterialDeliveryItem) -> MaterialInspection:
    service = MaterialInspectionService()
    request = service.request_inspection(session, item.delivery_item_id)
    stored = service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=request.inspection_request_id,
        delivery_item_id=request.delivery_item_id,
        inspection_cycle=request.inspection_cycle,
        status="COMPLETED",
        result="PASS",
        failure_type=None,
        expected_part_code=request.expected_part_code,
        expected_class_name=request.expected_class_name,
        expected_quantity=request.expected_quantity,
        detected_quantity=request.expected_quantity,
        detections=[QAWireDetection(
            class_name=request.expected_class_name,
            class_id=1,
            confidence=0.99,
            bbox_xyxy=[1.0, 1.0, 2.0, 2.0],
        )],
        frame_width=640,
        frame_height=480,
        camera_source="GLOBAL_CAMERA",
        frame_seq=request.inspection_cycle,
        timestamp=datetime.now(timezone.utc),
        model_scope="house_b",
        model_version="test",
        production_valid=True,
    ))
    session.commit()
    return stored


def test_house_b_global_preproduction_qa_gate_requires_actual_all_seven_items_and_keeps_parallelism(session: Session) -> None:
    job = _create_job(session)
    deliveries = {
        delivery.supply_group_code: delivery
        for delivery in session.scalars(select(JobMaterialDelivery).where(
            JobMaterialDelivery.production_job_id == job.job_id
        ))
    }
    items = {
        item.part_code: item
        for item in session.scalars(
            select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job.job_id)
        )
    }
    steps = {
        step.operation_code: step
        for step in session.scalars(select(JobStep).where(JobStep.job_id == job.job_id))
    }
    assert len(items) == 7
    assert items["ROOF-ZIP-01"].job_step_id is None

    # Independent durable physical facts do not bypass the House-B-wide QA gate.
    deliveries["BASE"].manual_prestage_ready_at = datetime.now(timezone.utc)
    deliveries["INNER_WALL"].physical_ready_at = datetime.now(timezone.utc)
    session.commit()
    readiness = StepReadinessService(MaterialDeliveryService(session))
    base = steps["INSTALL_BASE"]
    assert readiness.evaluate(job_id=job.job_id, job_step_id=base.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
    assert TransportEligibilityService(session).evaluate_transport_eligibility(
        job_delivery_id=deliveries["INNER_WALL"].job_delivery_id
    ).reason is TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE

    # No execution order is encoded here: this deliberately non-process order
    # leaves Roof uninspected until last.
    arbitrary_order = [
        "WALL-EXT-RIGHT-01",
        "BASE-HOUSE-B-01",
        "WALL-INT-HOUSE-B-01",
        "WALL-EXT-DOOR-01",
        "WALL-EXT-LEFT-WINDOW-01",
        "WALL-EXT-BACK-WINDOW-01",
    ]
    for part_code in arbitrary_order:
        _pass_item(session, items[part_code])
    state = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id)
    assert (state.required, state.ready, state.total_items, state.released_items) == (True, False, 7, 6)
    assert readiness.evaluate(job_id=job.job_id, job_step_id=base.job_step_id).reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE

    _pass_item(session, items["ROOF-ZIP-01"])
    state = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id)
    assert (state.required, state.ready, state.total_items, state.released_items) == (True, True, 7, 7)
    assert readiness.evaluate(job_id=job.job_id, job_step_id=base.job_step_id).ready is True
    assert TransportEligibilityService(session).evaluate_transport_eligibility(
        job_delivery_id=deliveries["INNER_WALL"].job_delivery_id
    ).eligible is True

    # QA completion opens both start paths, but it does not alter installation
    # sequencing: Inner remains behind the Base JobStep.
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    with pytest.raises(Exception, match="Invalid job step state transition"):
        orchestration.start_step(steps["INSTALL_INNER_WALL"].job_step_id)
    assert not session.scalar(select(JobStep).where(
        JobStep.job_id == job.job_id,
        JobStep.operation_code == "INSTALL_ROOF",
    ))


def test_house_b_latest_reinspection_pass_replaces_prior_fail_for_global_gate(session: Session) -> None:
    job = _create_job(session)
    items = list(session.scalars(
        select(JobMaterialDeliveryItem)
        .join(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job.job_id)
        .order_by(JobMaterialDeliveryItem.delivery_item_id)
    ))
    failed_item = next(item for item in items if item.part_code == "ROOF-ZIP-01")
    for item in items:
        if item is not failed_item:
            _pass_item(session, item)

    service = MaterialInspectionService()
    first = service.request_inspection(session, failed_item.delivery_item_id)
    service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=first.inspection_request_id,
        delivery_item_id=first.delivery_item_id,
        inspection_cycle=first.inspection_cycle,
        status="COMPLETED", result="FAIL", failure_type="MISSING",
        expected_part_code=first.expected_part_code, expected_class_name=first.expected_class_name,
        expected_quantity=first.expected_quantity, detected_quantity=0, detections=[],
        frame_width=640, frame_height=480, camera_source="GLOBAL_CAMERA", frame_seq=1,
        timestamp=datetime.now(timezone.utc), model_scope="house_b", model_version="test", production_valid=False,
    ))
    session.commit()
    assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job.job_id) is False

    second = service.request_inspection(session, failed_item.delivery_item_id)
    assert second.inspection_cycle == 2
    assert second.inspection_request_id != first.inspection_request_id
    service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=second.inspection_request_id,
        delivery_item_id=second.delivery_item_id,
        inspection_cycle=second.inspection_cycle,
        status="COMPLETED", result="PASS", failure_type=None,
        expected_part_code=second.expected_part_code, expected_class_name=second.expected_class_name,
        expected_quantity=second.expected_quantity, detected_quantity=1,
        detections=[QAWireDetection(class_name=second.expected_class_name, class_id=1, confidence=0.99, bbox_xyxy=[1, 1, 2, 2])],
        frame_width=640, frame_height=480, camera_source="GLOBAL_CAMERA", frame_seq=2,
        timestamp=datetime.now(timezone.utc), model_scope="house_b", model_version="test", production_valid=True,
    ))
    session.commit()
    assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job.job_id) is True
