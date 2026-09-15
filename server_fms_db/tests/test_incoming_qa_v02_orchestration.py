from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_material_qa_udp_transport import (
    IncomingQAUdpRuntime,
    IncomingQAUdpRuntimeConfig,
)
from fms_server.incoming_qa_v02_orchestration_service import (
    IncomingQAV02ActiveInspectionError,
    IncomingQAV02OrchestrationError,
    IncomingQAV02FmsReconciler,
    IncomingQAV02OrchestrationService,
)
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    StepStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import IncomingQADefectCode, IncomingQARequestV02, IncomingQAResultItemV02, IncomingQAResultV02
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_v02_monitoring_service import IncomingQAV02MonitoringService
from shared.vision_recipe_mapping import IncomingQAInspectionMode
from shared.realtime.incoming_qa_events import set_incoming_qa_change_callback
from shared.realtime.production_events import set_production_change_callback

_HOUSE_B = (
    ("base_house_b", "BASE-B"),
    ("wall_ext_back_window", "B01-P"),
    ("wall_ext_door", "B02-P"),
    ("wall_ext_left_window", "B03-P"),
    ("wall_ext_right", "B04-P"),
    ("wall_int_house_b", "B05-P"),
    ("roof_zip", "B06-P"),
)


@pytest.fixture
def context() -> Iterator[tuple[sessionmaker[Session], int]]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        session.add(Product(product_code="HOUSE_B", product_name="House B", is_active=True))
        session.flush()
        job = ProductionJob(job_code="QA-V02-HOUSE-B", product_code="HOUSE_B", status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code="QA-V02", display_name="QA", status="PENDING")
        session.add(delivery)
        session.flush()
        for vision_class, code in _HOUSE_B:
            session.add(Part(part_code=code, part_name=code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA"))
        session.flush()
        session.add_all([
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=code, quantity=1)
            for _vision_class, code in _HOUSE_B
        ])
        session.commit()
        yield factory, job.job_id
    engine.dispose()


def _transactions(session: Session, job_id: int) -> list[IncomingQATransaction]:
    return list(session.scalars(select(IncomingQATransaction).where(
        IncomingQATransaction.production_job_id == job_id
    ).order_by(IncomingQATransaction.transaction_id)))




def _add_house_b_job(session: Session, *, job_code: str, status: JobStatus = JobStatus.RUNNING) -> ProductionJob:
    job = ProductionJob(job_code=job_code, product_code="HOUSE_B", status=status)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"QA-{job_code}",
        display_name="QA", status="PENDING",
    )
    session.add(delivery)
    session.flush()
    session.add_all([
        JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=code, quantity=1)
        for _vision_class, code in _HOUSE_B
    ])
    session.flush()
    return job

def _inspection_items(session: Session, transaction: IncomingQATransaction) -> list[MaterialInspection]:
    return list(session.scalars(select(MaterialInspection).where(
        MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id
    ).order_by(MaterialInspection.delivery_item_id)))


def _runtime(factory: sessionmaker[Session]) -> IncomingQAUdpRuntime:
    return IncomingQAUdpRuntime(
        session_factory=factory,
        config=IncomingQAUdpRuntimeConfig(
            vision_host="127.0.0.1", vision_port=9, result_host="127.0.0.1", result_port=0,
            ack_timeout_seconds=1, max_retries=0,
        ),
    )


def _result(transaction: IncomingQATransaction, *, overall: str = "PASS", valid: bool = True) -> IncomingQAResultV02:
    request = IncomingQARequestV02.model_validate_json(transaction.immutable_request_snapshot)
    items = [IncomingQAResultItemV02(
        **item.model_dump(), predicted_class_name=item.expected_class_name,
        material_confidence=0.99, detected_quantity=item.expected_quantity, result="PASS",
    ) for item in request.items]
    if overall != "PASS":
        original = request.items[0]
        items[0] = IncomingQAResultItemV02(
            **original.model_dump(), predicted_class_name=original.expected_class_name,
            material_confidence=0.1, detected_quantity=0, result=overall,
            failure_type="MISSING" if overall == "FAIL" else None,
            defects=["COMPONENT_MISSING"] if overall == "FAIL" else [],
        )
    return IncomingQAResultV02(
        inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle,
        inspection_mode=request.inspection_mode, result=overall, production_valid=valid, items=items,
        camera_source="GLOBAL_CAMERA", timestamp="2026-09-04T04:00:00Z",
        model_scope="test", model_version="v0.2-test",
    )


def _complete(factory: sessionmaker[Session], transaction: IncomingQATransaction, *, overall: str = "PASS", valid: bool = True) -> None:
    assert asyncio.run(_runtime(factory).handle_result(_result(transaction, overall=overall, valid=valid)))


def test_house_b_initial_base_then_house_b_and_gate_release(context) -> None:
    factory, job_id = context
    with factory() as session:
        initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        assert initial.send_transaction_id is not None
        base = _transactions(session, job_id)[0]
        assert (base.inspection_mode, base.inspection_cycle) == ("BASE_AB", 1)
        assert [inspection.delivery_item_id for inspection in base.inspections]
        assert len(base.inspections) == 1
    _complete(factory, base)
    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        assert outcome.send_transaction_id is not None
        transactions = _transactions(session, job_id)
        assert len(transactions) == 2
        house = transactions[1]
        assert (house.inspection_mode, house.inspection_cycle, len(house.inspections)) == ("HOUSE_B", 1, 6)
    _complete(factory, house)
    with factory() as session:
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is True and readiness.released_items == 7 and readiness.total_items == 7


@pytest.mark.parametrize("overall,valid", [("FAIL", False), ("NOT_EVALUATED", False)])
def test_base_terminal_progression_requires_pass(context, overall: str, valid: bool) -> None:
    factory, job_id = context
    with factory() as session:
        IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base, overall=overall, valid=valid)
    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        transactions = _transactions(session, job_id)
        assert len(transactions) == 1
        assert outcome.blocked_reason == "BASE_PASS_REQUIRED"
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is False and readiness.released_items == 0 and readiness.total_items == 7


def test_duplicate_reconciliation_and_restart_gap_create_followup_once(context) -> None:
    factory, job_id = context
    with factory() as session:
        IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base)
    with factory() as session:
        first = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
    with factory() as session:
        second = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        transactions = _transactions(session, job_id)
        assert len(transactions) == 2
        assert first.send_transaction_id == second.send_transaction_id == transactions[-1].transaction_id


def test_reinspection_groups_only_same_mode_and_next_cycle(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base)
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.reconcile_job(job_id=job_id)
        house = _transactions(session, job_id)[1]
    _complete(factory, house)
    with factory() as session:
        b_items = {inspection.expected_class_name: inspection.delivery_item_id for inspection in _inspection_items(session, house)}
        b02 = b_items["wall_ext_door"]
        b03 = b_items["wall_ext_left_window"]
        first = IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=[b02]
        )
        tx_cycle2 = session.get(IncomingQATransaction, first.send_transaction_id)
        assert tx_cycle2 is not None and tx_cycle2.inspection_cycle == 2
    _complete(factory, tx_cycle2)
    with factory() as session:
        planned = IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=[b02, b03]
        )
        groups = [session.get(IncomingQATransaction, tx_id) for tx_id in planned.created_transaction_ids]
        assert [(group.inspection_mode, group.inspection_cycle, len(group.inspections)) for group in groups if group] == [
            ("HOUSE_B", 2, 1), ("HOUSE_B", 3, 1)
        ]


def test_full_reinspection_groups_all_equal_cycles_and_pass_items_are_allowed(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base)
    with factory() as session:
        IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        house = _transactions(session, job_id)[1]
    _complete(factory, house)
    with factory() as session:
        all_ids = [inspection.delivery_item_id for inspection in _inspection_items(session, house)]
        planned = IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=all_ids
        )
        assert len(planned.created_transaction_ids) == 1
        group = session.get(IncomingQATransaction, planned.created_transaction_ids[0])
        assert group is not None
        assert (group.inspection_mode, group.inspection_cycle, len(group.inspections)) == ("HOUSE_B", 2, 6)


def test_selected_queued_item_cannot_allocate_duplicate_cycle(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base)
    with factory() as session:
        IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        house = _transactions(session, job_id)[1]
    _complete(factory, house)
    with factory() as session:
        item_id = _inspection_items(session, house)[0].delivery_item_id
        IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=[item_id]
        )
    with factory() as session:
        with pytest.raises(IncomingQAV02ActiveInspectionError):
            IncomingQAV02OrchestrationService(session).request_reinspection(
                job_id=job_id, delivery_item_ids=[item_id]
            )
        histories = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.delivery_item_id == item_id
        )))
        assert sorted(inspection.inspection_cycle for inspection in histories) == [1, 2]


def test_house_b_mixed_result_keeps_pass_items_released_and_b02_reinspection_releases_gate(context) -> None:
    factory, job_id = context
    with factory() as session:
        IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base)
    with factory() as session:
        IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        house = _transactions(session, job_id)[1]

    initial = _result(house)
    b02 = next(item for item in initial.items if item.slot_id == "B02")
    failed_b02 = b02.model_copy(update={
        "predicted_class_name": b02.expected_class_name,
        "material_confidence": 0.10,
        "detected_quantity": 0,
        "result": "FAIL",
        "failure_type": "DEFECT",
        "defects": [IncomingQADefectCode.COLOR_NG],
        "quality_scores": {"surface": 0.10},
    })
    mixed = initial.model_copy(update={
        "result": "FAIL",
        "production_valid": False,
        "items": [failed_b02 if item.slot_id == "B02" else item for item in initial.items],
    })
    assert asyncio.run(_runtime(factory).handle_result(mixed))

    with factory() as session:
        transaction = session.get(IncomingQATransaction, house.transaction_id)
        assert transaction is not None
        assert transaction.overall_result is MaterialInspectionResult.FAIL
        assert transaction.production_valid is False
        inspections = _inspection_items(session, transaction)
        by_slot = {json.loads(inspection.result_detail_json or "{}")["slot_id"]: inspection for inspection in inspections}
        assert by_slot["B02"].result is MaterialInspectionResult.FAIL
        assert by_slot["B02"].production_valid is False
        assert all(inspection.production_valid is True for slot, inspection in by_slot.items() if slot != "B02")
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is False and readiness.released_items == 6 and readiness.total_items == 7
        b02_item_id = by_slot["B02"].delivery_item_id
        reinspection = IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=[b02_item_id]
        )
        cycle2 = session.get(IncomingQATransaction, reinspection.send_transaction_id)
        assert cycle2 is not None and cycle2.inspection_cycle == 2
    _complete(factory, cycle2)
    with factory() as session:
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is True and readiness.released_items == 7 and readiness.total_items == 7



@pytest.mark.parametrize("terminal_status", [IncomingQATransactionStatus.ERROR, IncomingQATransactionStatus.REJECTED])
def test_terminal_transport_failure_item_can_create_new_v02_reinspection_cycle(context, terminal_status) -> None:
    factory, job_id = context
    with factory() as session:
        initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        transaction = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert transaction is not None
        transaction.status = terminal_status
        transaction.error_reason = "ACK_TIMEOUT_MAX_RETRIES" if terminal_status is IncomingQATransactionStatus.ERROR else None
        if terminal_status is IncomingQATransactionStatus.REJECTED:
            transaction.ack_accepted = False
            transaction.ack_reason_code = "CONTRACT_CONFLICT"
        base = _inspection_items(session, transaction)[0]
        base.status = MaterialInspectionStatus.ERROR
        base.failure_reason = (
            "ACK_REJECTED:CONTRACT_CONFLICT"
            if terminal_status is IncomingQATransactionStatus.REJECTED
            else "ACK_TIMEOUT_MAX_RETRIES"
        )
        session.commit()
        original_request_id = transaction.inspection_request_id
        base_item_id = base.delivery_item_id

    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).request_reinspection(
            job_id=job_id, delivery_item_ids=[base_item_id]
        )
        replacement = session.get(IncomingQATransaction, outcome.send_transaction_id)
        assert replacement is not None
        assert (replacement.inspection_mode, replacement.inspection_cycle) == ("BASE_AB", 2)
        assert replacement.inspection_request_id != original_request_id
        assert [item.status for item in _inspection_items(session, replacement)] == [MaterialInspectionStatus.REQUESTED]


def test_base_reinspection_must_pass_before_house_b_progression(context) -> None:
    factory, job_id = context
    with factory() as session:
        IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
        base_cycle1 = _transactions(session, job_id)[0]
    _complete(factory, base_cycle1, overall="FAIL", valid=False)

    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        assert service.reconcile_job(job_id=job_id).blocked_reason == "BASE_PASS_REQUIRED"
        assert len(_transactions(session, job_id)) == 1
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is False and readiness.released_items == 0 and readiness.total_items == 7
        base_item_id = _inspection_items(session, base_cycle1)[0].delivery_item_id
        cycle2_plan = service.request_reinspection(
            job_id=job_id, delivery_item_ids=[base_item_id]
        )
        base_cycle2 = session.get(IncomingQATransaction, cycle2_plan.send_transaction_id)
        assert base_cycle2 is not None
        assert (base_cycle2.inspection_mode, base_cycle2.inspection_cycle) == ("BASE_AB", 2)
        assert base_cycle2.inspection_request_id != base_cycle1.inspection_request_id
    _complete(factory, base_cycle2, overall="FAIL", valid=False)

    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        assert service.reconcile_job(job_id=job_id).blocked_reason == "BASE_PASS_REQUIRED"
        assert [tx.inspection_mode for tx in _transactions(session, job_id)] == ["BASE_AB", "BASE_AB"]
        base_item_id = _inspection_items(session, base_cycle2)[0].delivery_item_id
        cycle3_plan = service.request_reinspection(
            job_id=job_id, delivery_item_ids=[base_item_id]
        )
        base_cycle3 = session.get(IncomingQATransaction, cycle3_plan.send_transaction_id)
        assert base_cycle3 is not None
        assert (base_cycle3.inspection_mode, base_cycle3.inspection_cycle) == ("BASE_AB", 3)
    _complete(factory, base_cycle3, valid=False)

    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        first = service.reconcile_job(job_id=job_id)
        second = service.reconcile_job(job_id=job_id)
        transactions = _transactions(session, job_id)
        house = transactions[-1]
        assert (house.inspection_mode, house.inspection_cycle, len(house.inspections)) == ("HOUSE_B", 1, 6)
        assert first.send_transaction_id == second.send_transaction_id == house.transaction_id
        assert [(tx.inspection_mode, tx.inspection_cycle) for tx in transactions] == [
            ("BASE_AB", 1), ("BASE_AB", 2), ("BASE_AB", 3), ("HOUSE_B", 1),
        ]
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is False and readiness.released_items == 1 and readiness.total_items == 7
    _complete(factory, house)
    with factory() as session:
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is True and readiness.released_items == 7 and readiness.total_items == 7

@pytest.mark.parametrize(
    ("parent_status", "blocks"),
    [
        (JobStatus.RUNNING, True),
        (JobStatus.CANCELED, False),
        (JobStatus.FAILED, False),
        (JobStatus.COMPLETED, False),
    ],
)
def test_global_transaction_guard_ignores_terminal_parent_jobs(context, parent_status: JobStatus, blocks: bool) -> None:
    factory, job_a_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.start_initial_inspection(job_id=job_a_id)
        transaction = _transactions(session, job_a_id)[0]
        transaction.status = IncomingQATransactionStatus.ACKED
        session.get(ProductionJob, job_a_id).status = parent_status
        job_b = _add_house_b_job(session, job_code=f"QA-GLOBAL-TX-{parent_status.value}")
        session.commit()

    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_b.job_id)
        historical = _transactions(session, job_a_id)[0]
        assert historical.status is IncomingQATransactionStatus.ACKED
        if blocks:
            assert outcome.blocked_reason == "ACTIVE_INCOMING_QA_TRANSACTION"
            assert outcome.created_transaction_ids == ()
        else:
            assert outcome.blocked_reason is None
            assert len(outcome.created_transaction_ids) == 1

@pytest.mark.parametrize(
    ("parent_status", "blocks"),
    [
        (JobStatus.RUNNING, True),
        (JobStatus.CANCELED, False),
        (JobStatus.FAILED, False),
        (JobStatus.COMPLETED, False),
    ],
)
def test_legacy_global_guard_ignores_terminal_parent_jobs(context, parent_status: JobStatus, blocks: bool) -> None:
    factory, job_a_id = context
    with factory() as session:
        item = session.scalar(
            select(JobMaterialDeliveryItem)
            .join(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job_a_id)
        )
        assert item is not None
        legacy = MaterialInspection(
            inspection_request_id=f"legacy-{parent_status.value}",
            delivery_item_id=item.delivery_item_id,
            inspection_cycle=1,
            status=MaterialInspectionStatus.REQUESTED,
            expected_part_code=item.part_code,
            expected_class_name="base_house_b",
            expected_quantity=1,
        )
        session.add(legacy)
        session.get(ProductionJob, job_a_id).status = parent_status
        job_b = _add_house_b_job(session, job_code=f"QA-GLOBAL-LEGACY-{parent_status.value}")
        session.commit()

    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_b.job_id)
        if blocks:
            assert outcome.blocked_reason == "ACTIVE_INCOMING_QA_TRANSACTION"
            assert outcome.created_transaction_ids == ()
        else:
            assert outcome.blocked_reason is None
            assert len(outcome.created_transaction_ids) == 1


def test_reconciler_skips_terminal_history_and_resumes_base_pass_followup(context) -> None:
    factory, active_job_id = context
    with factory() as session:
        terminal_job = _add_house_b_job(
            session, job_code="QA-TERMINAL-HISTORY", status=JobStatus.CANCELED
        )
        session.add(IncomingQATransaction(
            inspection_request_id="terminal-history-request",
            production_job_id=terminal_job.job_id,
            inspection_mode="BASE_AB",
            inspection_cycle=1,
            status=IncomingQATransactionStatus.ACKED,
            immutable_request_snapshot="{}",
        ))
        initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(
            job_id=active_job_id
        )
        base = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert base is not None
    _complete(factory, base)

    # The terminal Job has historical ACKED QA, but must not enter the FMS
    # reconciliation candidate set or starve this active BASE-PASS followup.
    with factory() as session:
        candidate_ids = list(session.scalars(
            IncomingQAV02FmsReconciler._candidate_job_ids_query()
        ))
        assert candidate_ids == [active_job_id]
    transaction_id = IncomingQAV02FmsReconciler(factory).reconcile_once()
    assert transaction_id is not None
    with factory() as session:
        transactions = _transactions(session, active_job_id)
        assert [(tx.inspection_mode, tx.inspection_cycle) for tx in transactions] == [
            ("BASE_AB", 1),
            ("HOUSE_B", 1),
        ]


def test_expected_item_lock_targets_only_mutable_delivery_items() -> None:
    sql = str(
        IncomingQAV02OrchestrationService._expected_items_query(123).compile(
            dialect=postgresql.dialect()
        )
    )
    lock_clause = sql.upper().split("FOR UPDATE", 1)[1]
    assert "JOB_MATERIAL_DELIVERY_ITEMS" in lock_clause
    assert "PARTS" not in lock_clause


def test_base_and_house_b_pass_with_runtime_unauthorized_metadata_release_gate(context) -> None:
    factory, job_id = context
    with factory() as session:
        initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(
            job_id=job_id
        )
        base = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert base is not None
    _complete(factory, base, valid=False)

    with factory() as session:
        first = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        second = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        transactions = _transactions(session, job_id)
        assert len(transactions) == 2
        house = transactions[-1]
        assert house.inspection_mode == "HOUSE_B"
        assert first.send_transaction_id == second.send_transaction_id == house.transaction_id
        persisted_base = transactions[0]
        assert persisted_base.overall_result is MaterialInspectionResult.PASS
        assert persisted_base.production_valid is False
    _complete(factory, house, valid=False)

    with factory() as session:
        transactions = _transactions(session, job_id)
        assert transactions[-1].overall_result is MaterialInspectionResult.PASS
        assert transactions[-1].production_valid is False
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.ready is True
        assert (readiness.released_items, readiness.total_items) == (7, 7)


def test_base_fail_with_runtime_unauthorized_metadata_never_creates_house_b(context) -> None:
    factory, job_id = context
    with factory() as session:
        initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(
            job_id=job_id
        )
        base = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert base is not None
    _complete(factory, base, overall="FAIL", valid=False)

    with factory() as session:
        outcome = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        assert outcome.blocked_reason == "BASE_PASS_REQUIRED"
        assert [tx.inspection_mode for tx in _transactions(session, job_id)] == ["BASE_AB"]


def test_operator_house_b_mode_reinspection_creates_new_full_cycle_and_fms_can_send(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        base_plan = service.start_initial_inspection(job_id=job_id)
        base = session.get(IncomingQATransaction, base_plan.send_transaction_id)
        assert base is not None
    _complete(factory, base)
    with factory() as session:
        house_plan = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
        house = session.get(IncomingQATransaction, house_plan.send_transaction_id)
        assert house is not None
    _complete(factory, house)

    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        cycle2_plan = service.request_mode_reinspection(
            job_id=job_id, inspection_mode=IncomingQAInspectionMode.HOUSE_B
        )
        cycle2 = session.get(IncomingQATransaction, cycle2_plan.send_transaction_id)
        assert cycle2 is not None
        assert (cycle2.inspection_mode, cycle2.inspection_cycle, cycle2.status) == (
            "HOUSE_B", 2, IncomingQATransactionStatus.REQUESTED
        )
        assert cycle2.inspection_request_id != house.inspection_request_id
        persisted_house = session.get(IncomingQATransaction, house.transaction_id)
        assert persisted_house is not None
        assert persisted_house.status is IncomingQATransactionStatus.COMPLETED
        assert persisted_house.overall_result is MaterialInspectionResult.PASS
        assert len(cycle2.inspections) == 6
        # The new latest item cycle holds the gate until its Final Result.
        assert IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id).ready is False
        monitor = IncomingQAV02MonitoringService(session).get_job_monitor(job_id=job_id)
        assert not any(transaction.can_reinspect_mode for transaction in monitor.transactions)
        with pytest.raises(IncomingQAV02ActiveInspectionError):
            service.request_mode_reinspection(
                job_id=job_id, inspection_mode=IncomingQAInspectionMode.HOUSE_B
            )
        # REQUESTED is durable; FMS reconciliation, not the operator endpoint, selects it for send.
        assert service.reconcile_job(job_id=job_id).send_transaction_id == cycle2.transaction_id
    _complete(factory, cycle2)

    with factory() as session:
        monitor = IncomingQAV02MonitoringService(session).get_job_monitor(job_id=job_id)
        assert next(
            transaction for transaction in monitor.transactions
            if transaction.inspection_mode == "HOUSE_B" and transaction.inspection_cycle == 2
        ).can_reinspect_mode is True
        service = IncomingQAV02OrchestrationService(session)
        cycle3_plan = service.request_mode_reinspection(
            job_id=job_id, inspection_mode=IncomingQAInspectionMode.HOUSE_B
        )
        cycle3 = session.get(IncomingQATransaction, cycle3_plan.send_transaction_id)
        assert cycle3 is not None
        assert cycle3.inspection_cycle == 3
        assert cycle3.inspection_request_id not in {house.inspection_request_id, cycle2.inspection_request_id}
        persisted_house = session.get(IncomingQATransaction, house.transaction_id)
        persisted_cycle2 = session.get(IncomingQATransaction, cycle2.transaction_id)
        assert persisted_house is not None and persisted_house.status is IncomingQATransactionStatus.COMPLETED
        assert persisted_cycle2 is not None and persisted_cycle2.status is IncomingQATransactionStatus.COMPLETED


def test_operator_base_mode_reinspection_is_safe_before_downstream_assembly(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        initial = service.start_initial_inspection(job_id=job_id)
        base = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert base is not None
    _complete(factory, base, overall="FAIL", valid=False)

    with factory() as session:
        next_plan = IncomingQAV02OrchestrationService(session).request_mode_reinspection(
            job_id=job_id, inspection_mode=IncomingQAInspectionMode.BASE_AB
        )
        cycle2 = session.get(IncomingQATransaction, next_plan.send_transaction_id)
        assert cycle2 is not None
        assert (cycle2.inspection_mode, cycle2.inspection_cycle) == ("BASE_AB", 2)
        assert cycle2.inspection_request_id != base.inspection_request_id


def test_operator_mode_reinspection_rejects_terminal_or_downstream_started_job(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        initial = service.start_initial_inspection(job_id=job_id)
        base = session.get(IncomingQATransaction, initial.send_transaction_id)
        assert base is not None
    _complete(factory, base)

    with factory() as session:
        job = session.get(ProductionJob, job_id)
        assert job is not None
        job.status = JobStatus.CANCELED
        session.commit()
        with pytest.raises(IncomingQAV02OrchestrationError, match="terminal production job"):
            IncomingQAV02OrchestrationService(session).request_mode_reinspection(
                job_id=job_id, inspection_mode=IncomingQAInspectionMode.BASE_AB
            )
        job.status = JobStatus.RUNNING
        session.add(JobStep(
            job_id=job_id, step_order=1, operation_code="INSTALL_TEST",
            display_name="Started", status=StepStatus.RUNNING,
        ))
        session.commit()
        with pytest.raises(IncomingQAV02OrchestrationError, match="downstream assembly"):
            IncomingQAV02OrchestrationService(session).request_mode_reinspection(
                job_id=job_id, inspection_mode=IncomingQAInspectionMode.BASE_AB
            )


def test_created_incoming_qa_transactions_notify_qa_and_production_after_commit(context) -> None:
    factory, job_id = context
    qa_events: list[int] = []
    production_events: list[tuple[int, str | None]] = []
    set_incoming_qa_change_callback(qa_events.append)
    set_production_change_callback(lambda observed_job_id, reason: production_events.append((observed_job_id, reason)))
    try:
        with factory() as session:
            created = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
            assert created.created_transaction_ids
            assert qa_events == list(created.created_transaction_ids)
            assert production_events == [(job_id, "incoming_qa_transaction_created")]
            base = session.get(IncomingQATransaction, created.send_transaction_id)
            assert base is not None
        _complete(factory, base, overall="FAIL", valid=False)
        qa_events.clear()
        production_events.clear()
        with factory() as session:
            created = IncomingQAV02OrchestrationService(session).request_mode_reinspection(
                job_id=job_id, inspection_mode=IncomingQAInspectionMode.BASE_AB
            )
            assert created.created_transaction_ids
            assert qa_events == list(created.created_transaction_ids)
            assert production_events == [(job_id, "incoming_qa_transaction_created")]
            assert len(_transactions(session, job_id)) == 2
    finally:
        set_incoming_qa_change_callback(None)
        set_production_change_callback(None)


def test_rejected_incoming_qa_operation_does_not_notify_production(context) -> None:
    factory, job_id = context
    qa_events: list[int] = []
    production_events: list[tuple[int, str | None]] = []
    set_incoming_qa_change_callback(qa_events.append)
    set_production_change_callback(lambda observed_job_id, reason: production_events.append((observed_job_id, reason)))
    try:
        with factory() as session:
            with pytest.raises(IncomingQAV02OrchestrationError, match="has not completed an initial cycle"):
                IncomingQAV02OrchestrationService(session).request_mode_reinspection(
                    job_id=job_id, inspection_mode=IncomingQAInspectionMode.BASE_AB
                )
        assert qa_events == []
        assert production_events == []
    finally:
        set_incoming_qa_change_callback(None)
        set_production_change_callback(None)


def test_held_house_b_advance_notifies_production_after_commit(context) -> None:
    factory, job_id = context
    qa_events: list[int] = []
    production_events: list[tuple[int, str | None]] = []
    set_incoming_qa_change_callback(qa_events.append)
    set_production_change_callback(lambda observed_job_id, reason: production_events.append((observed_job_id, reason)))
    try:
        with factory() as session:
            service = IncomingQAV02OrchestrationService(session)
            service.set_test_hold(job_id=job_id, enabled=True)
            initial = service.start_initial_inspection(job_id=job_id)
            base = session.get(IncomingQATransaction, initial.send_transaction_id)
            assert base is not None
        _complete(factory, base, overall="PASS", valid=False)
        qa_events.clear()
        production_events.clear()
        with factory() as session:
            created = IncomingQAV02OrchestrationService(session).advance_house_b_for_test_hold(job_id=job_id)
            assert qa_events == list(created.created_transaction_ids)
            assert production_events == [(job_id, "incoming_qa_house_b_advanced")]
            assert [transaction.inspection_mode for transaction in _transactions(session, job_id)] == ["BASE_AB", "HOUSE_B"]
    finally:
        set_incoming_qa_change_callback(None)
        set_production_change_callback(None)


def test_test_hold_suppresses_base_pass_followup_until_manual_advance(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        assert service.set_test_hold(job_id=job_id, enabled=True) is True
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base, overall="PASS", valid=False)
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        outcome = service.reconcile_job(job_id=job_id)
        assert outcome.blocked_reason == "INCOMING_QA_TEST_HOLD"
        assert [tx.inspection_mode for tx in _transactions(session, job_id)] == ["BASE_AB"]
        state = service.test_hold_state(job_id=job_id)
        assert state.enabled and state.can_advance_house_b
        created = service.advance_house_b_for_test_hold(job_id=job_id)
        assert len(created.created_transaction_ids) == 1
        assert session.get(ProductionJob, job_id).incoming_qa_test_hold is True
        house = session.get(IncomingQATransaction, created.send_transaction_id)
        assert house is not None and (house.inspection_mode, house.inspection_cycle, house.status) == (
            "HOUSE_B", 1, IncomingQATransactionStatus.REQUESTED
        )
        with pytest.raises(IncomingQAV02ActiveInspectionError):
            service.advance_house_b_for_test_hold(job_id=job_id)


def test_test_hold_keeps_house_pass_from_releasing_and_can_release(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.set_test_hold(job_id=job_id, enabled=True)
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
    _complete(factory, base, overall="PASS", valid=False)
    with factory() as session:
        house_id = IncomingQAV02OrchestrationService(session).advance_house_b_for_test_hold(
            job_id=job_id
        ).send_transaction_id
        house = session.get(IncomingQATransaction, house_id)
        assert house is not None
    _complete(factory, house, overall="PASS", valid=False)
    with factory() as session:
        readiness = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert readiness.qa_passed is True
        assert readiness.test_hold is True
        assert readiness.ready is False
        service = IncomingQAV02OrchestrationService(session)
        state = service.test_hold_state(job_id=job_id)
        assert state.can_release is True
        assert service.can_request_mode_reinspection(
            job_id=job_id, inspection_mode=IncomingQAInspectionMode.HOUSE_B
        ) is True
        assert service.set_test_hold(job_id=job_id, enabled=False) is False
        released = IncomingQAOrchestrationService(session).preproduction_readiness(job_id=job_id)
        assert released.qa_passed is True and released.ready is True and released.test_hold is False


def test_test_hold_allows_active_acked_but_rejects_downstream_or_terminal(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        service.start_initial_inspection(job_id=job_id)
        base = _transactions(session, job_id)[0]
        base.status = IncomingQATransactionStatus.ACKED
        session.commit()
        assert service.set_test_hold(job_id=job_id, enabled=True) is True
        job = session.get(ProductionJob, job_id)
        assert job is not None
        step = JobStep(job_id=job_id, step_order=1, operation_code="INSTALL_BASE", display_name="base", status=StepStatus.RUNNING)
        session.add(step)
        session.commit()
    with factory() as session:
        with pytest.raises(IncomingQAV02OrchestrationError):
            IncomingQAV02OrchestrationService(session).set_test_hold(job_id=job_id, enabled=False)
        step = session.scalar(select(JobStep).where(JobStep.job_id == job_id))
        assert step is not None
        step.status = StepStatus.PENDING
        job = session.get(ProductionJob, job_id)
        assert job is not None
        job.status = JobStatus.CANCELED
        session.commit()
    with factory() as session:
        with pytest.raises(IncomingQAV02OrchestrationError):
            IncomingQAV02OrchestrationService(session).set_test_hold(job_id=job_id, enabled=True)


def test_test_override_base_pass_respects_existing_incoming_qa_test_hold(context) -> None:
    factory, job_id = context
    with factory() as session:
        service = IncomingQAV02OrchestrationService(session)
        assert service.set_test_hold(job_id=job_id, enabled=True) is True
        transaction = service.complete_current_mode_for_test_override(job_id=job_id)
        assert (transaction.inspection_mode, transaction.inspection_cycle) == ("BASE_AB", 1)
        result = service.reconcile_job(job_id=job_id)
        assert result.blocked_reason == "INCOMING_QA_TEST_HOLD"
        assert [(tx.inspection_mode, tx.inspection_cycle) for tx in _transactions(session, job_id)] == [
            ("BASE_AB", 1)
        ]
