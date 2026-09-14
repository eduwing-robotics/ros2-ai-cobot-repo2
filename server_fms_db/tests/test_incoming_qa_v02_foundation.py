from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import (
    IncomingQAAckReasonCode,
    IncomingQAAckV02,
    IncomingQADefectCode,
    IncomingQARequestItemV02,
    IncomingQARequestV02,
    IncomingQAResultItemV02,
    IncomingQAResultV02,
    incoming_qa_result_item_evidence_json,
)
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_transaction_service import (
    IncomingQATransactionConflictError,
    IncomingQATransactionService,
)
from shared.services.material_inspection_service import MaterialInspectionService
from shared.vision_recipe_mapping import (
    IncomingQAInspectionMode,
    VisionRecipeMappingError,
    allowed_slots,
    expected_vision_class,
    find_mode_slot_for_vision_class,
    initial_mode_slots_for_product,
    resolve_product_mode_slot,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        product = Product(product_code="QA_V02_PRODUCT", product_name="QA v0.2 test product", is_active=True)
        db.add(product)
        db.flush()
        job = ProductionJob(job_code="QA-V02-JOB", product_code=product.product_code, status=JobStatus.REQUESTED)
        db.add(job)
        db.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id, batch_order=1, delivery_code="QA-V02", display_name="QA v0.2", status="PENDING",
        )
        db.add(delivery)
        db.flush()
        for code, vision_class in (("QA-B01", "wall_ext_back_window"), ("QA-B02", "wall_ext_door")):
            db.add(Part(part_code=code, part_name=code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA"))
        db.flush()
        db.add_all([
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code="QA-B01", quantity=1),
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code="QA-B02", quantity=1),
        ])
        db.commit()
        yield db
    engine.dispose()


def _request(session: Session, *, request_id: str = "REQ-V02-100", reverse: bool = False) -> IncomingQARequestV02:
    items = list(session.scalars(select(JobMaterialDeliveryItem).order_by(JobMaterialDeliveryItem.delivery_item_id)))
    values = [
        IncomingQARequestItemV02(slot_id="B01", delivery_item_id=items[0].delivery_item_id, expected_part_code="QA-B01", expected_class_name="wall_ext_back_window", expected_quantity=1),
        IncomingQARequestItemV02(slot_id="B02", delivery_item_id=items[1].delivery_item_id, expected_part_code="QA-B02", expected_class_name="wall_ext_door", expected_quantity=1),
    ]
    if reverse:
        values.reverse()
    return IncomingQARequestV02(
        inspection_request_id=request_id,
        inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        items=values,
    )


def test_v02_transaction_persists_multiple_item_histories_and_keeps_latest_gate(session: Session) -> None:
    job = session.scalar(select(ProductionJob))
    assert job is not None
    created = IncomingQATransactionService().create_or_get(session, production_job_id=job.job_id, request=_request(session))
    session.commit()

    assert created.created is True
    transaction = session.get(IncomingQATransaction, created.transaction.transaction_id)
    assert transaction is not None
    inspections = list(session.scalars(select(MaterialInspection).where(
        MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id
    ).order_by(MaterialInspection.delivery_item_id)))
    assert len(inspections) == 2
    assert {inspection.inspection_request_id for inspection in inspections} == {"REQ-V02-100"}
    assert all(inspection.incoming_qa_transaction_id == transaction.transaction_id for inspection in inspections)
    assert IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id).ready is False

    inspections[0].status = MaterialInspectionStatus.COMPLETED
    inspections[0].result = MaterialInspectionResult.PASS
    inspections[0].production_valid = True
    inspections[0].predicted_class_name = "wall_ext_back_window"
    inspections[0].material_confidence = 0.98
    inspections[0].result_detail_json = json.dumps({"defects": [], "quality_scores": {"surface": 0.98}})
    inspections[1].status = MaterialInspectionStatus.COMPLETED
    inspections[1].result = MaterialInspectionResult.FAIL
    inspections[1].production_valid = False
    session.commit()
    assert IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id).ready is False
    session.expire_all()
    evidence = session.get(MaterialInspection, inspections[0].inspection_id)
    assert evidence is not None
    assert (evidence.predicted_class_name, evidence.material_confidence) == ("wall_ext_back_window", 0.98)
    assert json.loads(evidence.result_detail_json or "{}") == {"defects": [], "quality_scores": {"surface": 0.98}}

    inspections[1].result = MaterialInspectionResult.PASS
    inspections[1].production_valid = True
    session.commit()
    assert IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job.job_id).ready is True


def test_v02_same_context_is_order_independent_but_conflict_is_rejected(session: Session) -> None:
    job = session.scalar(select(ProductionJob))
    assert job is not None
    service = IncomingQATransactionService()
    first = service.create_or_get(session, production_job_id=job.job_id, request=_request(session))
    same = service.create_or_get(session, production_job_id=job.job_id, request=_request(session, reverse=True))
    assert first.created is True
    assert same.created is False
    assert same.transaction.transaction_id == first.transaction.transaction_id

    changed = _request(session)
    changed.items[0].expected_quantity = 2
    with pytest.raises(IncomingQATransactionConflictError):
        service.create_or_get(session, production_job_id=job.job_id, request=changed)


def test_v02_item_result_evidence_is_lossless_and_core_values_are_queryable(session: Session) -> None:
    item = IncomingQAResultItemV02(
        slot_id="B01", delivery_item_id=1, expected_part_code="QA-B01", expected_class_name="wall_ext_back_window", expected_quantity=1,
        predicted_class_name="wall_ext_back_window", material_confidence=0.97, detected_quantity=1,
        result="FAIL", failure_type="DEFECT", defects=[IncomingQADefectCode.CRACK_DAMAGE, IncomingQADefectCode.COLOR_NG],
        quality_scores={"surface": 0.2, "geometry": 0.8},
    )
    result = IncomingQAResultV02(
        inspection_request_id="REQ-RESULT", inspection_cycle=1, inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        result="FAIL", production_valid=False, items=[item], camera_source="GLOBAL_CAMERA", timestamp="2026-09-04T00:00:00Z", model_scope="house_b", model_version="v0.2-test",
    )
    assert result.items[0].defects == [IncomingQADefectCode.CRACK_DAMAGE, IncomingQADefectCode.COLOR_NG]
    evidence = incoming_qa_result_item_evidence_json(item)
    assert evidence["quality_scores"] == {"surface": 0.2, "geometry": 0.8}
    assert evidence["defects"] == ["CRACK_DAMAGE", "COLOR_NG"]


def test_v02_item_result_uses_the_frozen_wire_shape_without_production_valid() -> None:
    payload = {
        "slot_id": "B01", "delivery_item_id": 1, "expected_part_code": "P",
        "expected_class_name": "wall_ext_back_window", "expected_quantity": 1,
        "detected_quantity": 1, "result": "PASS",
    }
    item = IncomingQAResultItemV02.model_validate(payload)
    assert "production_valid" not in IncomingQAResultItemV02.model_fields
    assert "production_valid" not in item.model_dump()
    with pytest.raises(ValidationError):
        IncomingQAResultItemV02.model_validate({**payload, "production_valid": True})


def test_v02_schema_rejects_invalid_wire_context() -> None:
    with pytest.raises(ValidationError):
        IncomingQARequestV02(inspect_request_id="bad")
    with pytest.raises(ValidationError, match="Unsupported Vision Incoming-QA slot"):
        IncomingQARequestV02(
            inspection_request_id="bad-slot", inspection_cycle=1, inspection_mode=IncomingQAInspectionMode.HOUSE_B,
            items=[IncomingQARequestItemV02(slot_id="B99", delivery_item_id=1, expected_part_code="P", expected_class_name="wall_ext_back_window", expected_quantity=1)],
        )
    with pytest.raises(ValidationError):
        IncomingQARequestV02(
            inspection_request_id="empty", inspection_cycle=1, inspection_mode=IncomingQAInspectionMode.HOUSE_B, items=[]
        )
    with pytest.raises(ValidationError):
        IncomingQAAckV02(inspection_request_id="ack", inspection_cycle=1, accepted=False, duplicate=False)
    rejected = IncomingQAAckV02(
        inspection_request_id="ack", inspection_cycle=1, accepted=False, duplicate=False,
        reason_code=IncomingQAAckReasonCode.CONTRACT_CONFLICT,
    )
    assert rejected.reason_code is IncomingQAAckReasonCode.CONTRACT_CONFLICT


@pytest.mark.parametrize(("mode", "slots"), [
    (IncomingQAInspectionMode.BASE_AB, ("C08", "C09")),
    (IncomingQAInspectionMode.HOUSE_B, ("B01", "B02", "B03", "B04", "B05", "B06")),
    (IncomingQAInspectionMode.HOUSE_A, ("A01", "A02", "A03", "A04", "A05", "A06", "A07")),
])
def test_v02_mapping_covers_contract_modes(mode: IncomingQAInspectionMode, slots: tuple[str, ...]) -> None:
    assert allowed_slots(mode) == slots
    for slot in slots:
        vision_class = expected_vision_class(mode=mode, slot_id=slot)
        assert find_mode_slot_for_vision_class(mode=mode, vision_class=vision_class) == slot
    with pytest.raises(VisionRecipeMappingError):
        expected_vision_class(mode=mode, slot_id="INVALID")


def test_legacy_single_item_http_service_row_remains_transaction_null(session: Session) -> None:
    item = session.scalar(select(JobMaterialDeliveryItem).order_by(JobMaterialDeliveryItem.delivery_item_id))
    assert item is not None
    request = MaterialInspectionService().request_inspection(session, item.delivery_item_id)
    session.commit()
    persisted = session.scalar(select(MaterialInspection).where(
        MaterialInspection.inspection_request_id == request.inspection_request_id
    ))
    assert persisted is not None
    assert persisted.incoming_qa_transaction_id is None


def test_v02_valid_duplicate_ack_and_mixed_item_result() -> None:
    ack = IncomingQAAckV02(
        inspection_request_id="REQ-ACK", inspection_cycle=2, accepted=True, duplicate=True
    )
    assert ack.accepted is True and ack.duplicate is True
    mixed = IncomingQAResultV02(
        inspection_request_id="REQ-MIXED", inspection_cycle=2, inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        result="FAIL", production_valid=False, camera_source="GLOBAL_CAMERA",
        timestamp="2026-09-04T00:00:00Z", model_scope="house_b", model_version="v0.2-test",
        items=[
            IncomingQAResultItemV02(
                slot_id="B01", delivery_item_id=11, expected_part_code="P1", expected_class_name="wall_ext_back_window",
                expected_quantity=1, detected_quantity=1, result="PASS"
            ),
            IncomingQAResultItemV02(
                slot_id="B02", delivery_item_id=12, expected_part_code="P2", expected_class_name="wall_ext_door",
                expected_quantity=1, predicted_class_name="wall_ext_door", material_confidence=0.4, detected_quantity=0,
                result="FAIL", failure_type="MISSING", defects=[IncomingQADefectCode.COMPONENT_MISSING],
            ),
        ],
    )
    assert [item.result for item in mixed.items] == ["PASS", "FAIL"]


def test_v02_item_failure_type_depends_on_result_semantics() -> None:
    common = {
        "slot_id": "B01", "delivery_item_id": 11, "expected_part_code": "P1",
        "expected_class_name": "wall_ext_back_window", "expected_quantity": 1,
        "detected_quantity": 0,
    }
    not_evaluated = IncomingQAResultItemV02(**common, result="NOT_EVALUATED")
    assert not_evaluated.failure_type is None

    with pytest.raises(ValidationError):
        IncomingQAResultItemV02(**common, result="NOT_EVALUATED", failure_type="MISSING")
    with pytest.raises(ValidationError):
        IncomingQAResultItemV02(**common, result="FAIL")
    with pytest.raises(ValidationError):
        IncomingQAResultItemV02(**common, result="PASS", failure_type="DEFECT")


@pytest.mark.parametrize(("mode", "slot_id", "vision_class"), [
    (IncomingQAInspectionMode.BASE_AB, "C08", "base_house_a"),
    (IncomingQAInspectionMode.BASE_AB, "C09", "base_house_b"),
    (IncomingQAInspectionMode.HOUSE_B, "B01", "wall_ext_back_window"),
    (IncomingQAInspectionMode.HOUSE_B, "B02", "wall_ext_door"),
    (IncomingQAInspectionMode.HOUSE_B, "B03", "wall_ext_left_window"),
    (IncomingQAInspectionMode.HOUSE_B, "B04", "wall_ext_right"),
    (IncomingQAInspectionMode.HOUSE_B, "B05", "wall_int_house_b"),
    (IncomingQAInspectionMode.HOUSE_B, "B06", "roof_zip"),
    (IncomingQAInspectionMode.HOUSE_A, "A01", "wall_ext_back_window"),
    (IncomingQAInspectionMode.HOUSE_A, "A02", "wall_ext_door"),
    (IncomingQAInspectionMode.HOUSE_A, "A03", "wall_ext_left_window"),
    (IncomingQAInspectionMode.HOUSE_A, "A04", "wall_ext_right"),
    (IncomingQAInspectionMode.HOUSE_A, "A05", "wall_int_house_a"),
    (IncomingQAInspectionMode.HOUSE_A, "A06", "roof_nozip"),
    (IncomingQAInspectionMode.HOUSE_A, "A07", "wall_int_house_a_door"),
])
def test_v02_mapping_exact_contract(mode: IncomingQAInspectionMode, slot_id: str, vision_class: str) -> None:
    assert expected_vision_class(mode=mode, slot_id=slot_id) == vision_class


def test_v02_schema_rejects_wrong_version_message_type_and_mode() -> None:
    item = {
        "slot_id": "B01", "delivery_item_id": 1, "expected_part_code": "P",
        "expected_class_name": "wall_ext_back_window", "expected_quantity": 1,
    }
    with pytest.raises(ValidationError):
        IncomingQARequestV02(ver="0.1", inspection_request_id="wrong-version", inspection_cycle=1, inspection_mode="HOUSE_B", items=[item])
    with pytest.raises(ValidationError):
        IncomingQARequestV02(message_type="incoming_qa_result", inspection_request_id="wrong-type", inspection_cycle=1, inspection_mode="HOUSE_B", items=[item])
    with pytest.raises(ValidationError):
        IncomingQARequestV02(inspection_request_id="wrong-mode", inspection_cycle=1, inspection_mode="UNKNOWN", items=[item])


def _wire_item(slot_id: str, vision_class: str, delivery_item_id: int) -> IncomingQARequestItemV02:
    return IncomingQARequestItemV02(
        slot_id=slot_id,
        delivery_item_id=delivery_item_id,
        expected_part_code=f"PART-{slot_id}",
        expected_class_name=vision_class,
        expected_quantity=1,
    )


def test_final_product_base_slots_are_product_specific_and_full_modes_are_distinct() -> None:
    assert initial_mode_slots_for_product("HOUSE_A") == (
        (IncomingQAInspectionMode.BASE_AB, ("C08",)),
        (IncomingQAInspectionMode.HOUSE_A, ("A01", "A02", "A03", "A04", "A05", "A06", "A07")),
    )
    assert initial_mode_slots_for_product("HOUSE_B") == (
        (IncomingQAInspectionMode.BASE_AB, ("C09",)),
        (IncomingQAInspectionMode.HOUSE_B, ("B01", "B02", "B03", "B04", "B05", "B06")),
    )


def test_final_contract_allows_subset_items_without_synthesizing_absent_slots() -> None:
    house_a_items = [
        _wire_item(slot_id, expected_vision_class(mode=IncomingQAInspectionMode.HOUSE_A, slot_id=slot_id), index)
        for index, slot_id in enumerate(allowed_slots(IncomingQAInspectionMode.HOUSE_A), start=1)
    ]
    full_a = IncomingQARequestV02(
        inspection_request_id="HOUSE-A-FULL", inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.HOUSE_A, items=house_a_items,
    )
    assert [item.slot_id for item in full_a.items] == ["A01", "A02", "A03", "A04", "A05", "A06", "A07"]
    single_a = IncomingQARequestV02(
        inspection_request_id="HOUSE-A-A02-REINSPECT", inspection_cycle=2,
        inspection_mode=IncomingQAInspectionMode.HOUSE_A,
        items=[_wire_item("A02", "wall_ext_door", 2)],
    )
    single_b = IncomingQARequestV02(
        inspection_request_id="HOUSE-B-B05-REINSPECT", inspection_cycle=2,
        inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        items=[_wire_item("B05", "wall_int_house_b", 5)],
    )
    base_a = IncomingQARequestV02(
        inspection_request_id="BASE-A", inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.BASE_AB,
        items=[_wire_item("C08", "base_house_a", 8)],
    )
    base_b = IncomingQARequestV02(
        inspection_request_id="BASE-B", inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.BASE_AB,
        items=[_wire_item("C09", "base_house_b", 9)],
    )
    assert [item.slot_id for item in single_a.items] == ["A02"]
    assert [item.slot_id for item in single_b.items] == ["B05"]
    assert [item.slot_id for item in base_a.items] == ["C08"]
    assert [item.slot_id for item in base_b.items] == ["C09"]


def test_stale_wall_ext_back_is_not_a_final_contract_alias() -> None:
    with pytest.raises(VisionRecipeMappingError):
        resolve_product_mode_slot(product_code="HOUSE_A", vision_class="wall_ext_back")
    assert resolve_product_mode_slot(product_code="HOUSE_A", vision_class="wall_ext_back_window") == (
        IncomingQAInspectionMode.HOUSE_A, "A01"
    )
    assert resolve_product_mode_slot(product_code="HOUSE_B", vision_class="wall_ext_back_window") == (
        IncomingQAInspectionMode.HOUSE_B, "B01"
    )
