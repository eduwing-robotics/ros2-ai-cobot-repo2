from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.services.unity_incoming_qa_projection_service import (
    UnityIncomingQAProjectionService,
)
from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from fms_server.incoming_qa_realtime_publisher import IncomingQAChangedPublisher
from fms_server.incoming_material_qa_udp_transport import (
    IncomingQACorrelationError,
    IncomingQAUdpRuntime,
    IncomingQAUdpRuntimeConfig,
)
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    Inventory,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionFailureType,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.realtime.incoming_qa_events import INCOMING_QA_CHANGED_CHANNEL, set_incoming_qa_change_callback
from shared.realtime.production_events import set_production_change_callback
from shared.schemas.vision import (
    IncomingQAAckV02,
    IncomingQARequestItemV02,
    IncomingQARequestV02,
    IncomingQAResultItemV02,
    IncomingQAResultV02,
)
from shared.services.incoming_qa_transaction_service import IncomingQATransactionService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from scripts.seed_house_b_mvp_master import HOUSE_B_PRODUCT_CODE, seed_house_b_mvp_master
from shared.models.factory import RoofOptionCode
from shared.vision_recipe_mapping import IncomingQAInspectionMode


@dataclass(eq=False)
class FakeWebSocket:
    messages: list[dict] = field(default_factory=list)

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _empty_snapshot() -> dict:
    return {"jobs": [], "robots": [], "transports": [], "incoming_qa": [], "active_errors": []}


def _seed(session: Session) -> tuple[ProductionJob, dict[str, JobMaterialDeliveryItem]]:
    product = Product(product_code="UNITY_QA", product_name="Unity QA", is_active=True)
    session.add(product)
    session.flush()
    job = ProductionJob(job_code="UNITY-QA-JOB", product_code=product.product_code, status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code="UNITY-QA-DELIVERY",
        display_name="Unity QA",
        status="PENDING",
    )
    session.add(delivery)
    session.flush()
    specs = {
        "C09": ("UNITY-C09", "base_house_b"),
        "B01": ("UNITY-B01", "wall_ext_back_window"),
        "B02": ("UNITY-B02", "wall_ext_door"),
    }
    for part_code, vision_class in specs.values():
        session.add(Part(
            part_code=part_code,
            part_name=part_code,
            category=PartCategory.STRUCTURE,
            vision_class=vision_class,
            unit="EA",
        ))
    session.flush()
    items = {
        slot: JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id,
            part_code=part_code,
            quantity=1,
        )
        for slot, (part_code, _vision_class) in specs.items()
    }
    session.add_all(items.values())
    session.flush()
    return job, items


def _create(
    session: Session,
    *,
    job: ProductionJob,
    request_id: str,
    mode: IncomingQAInspectionMode,
    cycle: int,
    items: list[tuple[str, JobMaterialDeliveryItem, str]],
) -> IncomingQATransaction:
    request = IncomingQARequestV02(
        inspection_request_id=request_id,
        inspection_mode=mode,
        inspection_cycle=cycle,
        items=[
            IncomingQARequestItemV02(
                slot_id=slot,
                delivery_item_id=item.delivery_item_id,
                expected_part_code=item.part_code,
                expected_class_name=vision_class,
                expected_quantity=item.quantity,
            )
            for slot, item, vision_class in items
        ],
    )
    return IncomingQATransactionService().create_or_get(
        session, production_job_id=job.job_id, request=request
    ).transaction


def _inspection(session: Session, transaction: IncomingQATransaction, item: JobMaterialDeliveryItem) -> MaterialInspection:
    inspection = session.scalar(select(MaterialInspection).where(
        MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id,
        MaterialInspection.delivery_item_id == item.delivery_item_id,
    ))
    assert inspection is not None
    return inspection


def test_unity_projection_uses_latest_effective_item_cycle_and_omits_raw_vision_detail() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            job, items = _seed(session)
            house_cycle1 = _create(
                session,
                job=job,
                request_id="UNITY-HOUSE-1",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=1,
                items=[
                    ("B01", items["B01"], "wall_ext_back_window"),
                    ("B02", items["B02"], "wall_ext_door"),
                ],
            )
            house_cycle1.status = IncomingQATransactionStatus.COMPLETED
            house_cycle1.overall_result = MaterialInspectionResult.FAIL
            house_cycle1.production_valid = False
            b01 = _inspection(session, house_cycle1, items["B01"])
            b01.status, b01.result, b01.production_valid = (
                MaterialInspectionStatus.COMPLETED,
                MaterialInspectionResult.PASS,
                True,
            )
            b02 = _inspection(session, house_cycle1, items["B02"])
            b02.status, b02.result, b02.production_valid = (
                MaterialInspectionStatus.COMPLETED,
                MaterialInspectionResult.FAIL,
                False,
            )
            b02.failure_type = MaterialInspectionFailureType.DEFECT
            b02.failure_reason = "Incomplete formation detected"
            b02.predicted_class_name = "wrong_raw_vision_value"
            b02.material_confidence = 0.12
            b02.result_detail_json = '{"defects":["COLOR_NG"],"quality_scores":{"surface":0.12}}'

            b02_cycle2 = _create(
                session,
                job=job,
                request_id="UNITY-B02-2",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=2,
                items=[("B02", items["B02"], "wall_ext_door")],
            )
            b02_cycle2.status = IncomingQATransactionStatus.COMPLETED
            b02_cycle2.overall_result = MaterialInspectionResult.PASS
            b02_cycle2.production_valid = True
            b02_latest = _inspection(session, b02_cycle2, items["B02"])
            b02_latest.status, b02_latest.result, b02_latest.production_valid = (
                MaterialInspectionStatus.COMPLETED,
                MaterialInspectionResult.PASS,
                True,
            )

            base_error = _create(
                session,
                job=job,
                request_id="UNITY-BASE-1",
                mode=IncomingQAInspectionMode.BASE_AB,
                cycle=1,
                items=[("C09", items["C09"], "base_house_b")],
            )
            base_error.status = IncomingQATransactionStatus.ERROR
            base_error.error_reason = "ACK_TIMEOUT_MAX_RETRIES"
            base_item = _inspection(session, base_error, items["C09"])
            base_item.status = MaterialInspectionStatus.ERROR
            base_item.failure_reason = "ACK_TIMEOUT_MAX_RETRIES"
            session.commit()

            projection = UnityIncomingQAProjectionService(session)
            live = projection.get_transaction_status(transaction_id=house_cycle1.transaction_id)
            assert live == {
                "job_id": job.job_id,
                "job_gate_state": "NOT_RELEASED",
                "transaction": {
                    "transaction_id": house_cycle1.transaction_id,
                    "request_id": "UNITY-HOUSE-1",
                    "mode": "HOUSE_B",
                    "cycle": 1,
                    "status": "COMPLETED",
                    "retry_count": 0,
                    "overall_result": "FAIL",
                    "production_valid": False,
                    "error_reason": None,
                },
                "items": [
                    {"delivery_item_id": items["B01"].delivery_item_id, "slot_id": "B01", "expected_part_code": "UNITY-B01", "status": "COMPLETED", "result": "PASS", "failure_type": None, "failure_reason": None, "defects": []},
                    {"delivery_item_id": items["B02"].delivery_item_id, "slot_id": "B02", "expected_part_code": "UNITY-B02", "status": "COMPLETED", "result": "FAIL", "failure_type": "DEFECT", "failure_reason": "Incomplete formation detected", "defects": ["COLOR_NG"]},
                ],
            }
            assert "predicted_class_name" not in str(live)
            assert "quality_scores" not in str(live)

            snapshot = projection.get_job_snapshot(job_id=job.job_id)
            assert snapshot is not None
            assert snapshot["job_gate_state"] == live["job_gate_state"] == "NOT_RELEASED"
            assert [(tx["mode"], tx["cycle"], tx["status"]) for tx in snapshot["transactions"]] == [
                ("HOUSE_B", 2, "COMPLETED"),
                ("BASE_AB", 1, "ERROR"),
            ]
            assert {item["slot_id"]: item["result"] for item in snapshot["items"]} == {
                "B01": "PASS", "B02": "PASS", "C09": None,
            }
            assert next(item for item in snapshot["items"] if item["slot_id"] == "C09")["status"] == "ERROR"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("status", ["REQUESTED", "SENT", "ACKED", "COMPLETED", "REJECTED", "ERROR"])
def test_subscriber_rereads_incoming_qa_status_and_broadcasts_to_all_unity_clients(status: str) -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(_empty_snapshot)
        first, second = FakeWebSocket(), FakeWebSocket()
        await hub.connect(first)
        await hub.connect(second)
        seen: list[int] = []

        def reader(transaction_id: int) -> dict:
            seen.append(transaction_id)
            return {
                "job_id": 19,
                "job_gate_state": "RELEASED" if status == "COMPLETED" else "NOT_RELEASED",
                "transaction": {
                    "transaction_id": transaction_id,
                    "request_id": "DB-AUTHORITY",
                    "mode": "HOUSE_B",
                    "cycle": 2,
                    "status": status,
                    "retry_count": 1,
                    "overall_result": "PASS" if status == "COMPLETED" else None,
                    "production_valid": True if status == "COMPLETED" else None,
                    "error_reason": "ACK_TIMEOUT_MAX_RETRIES" if status == "ERROR" else None,
                },
                "items": [{
                    "delivery_item_id": 31,
                    "slot_id": "B02",
                    "expected_part_code": "DB-B02",
                    "status": "ERROR" if status in {"ERROR", "REJECTED"} else status,
                    "result": None,
                    "failure_type": None,
                }],
            }

        subscriber = RedisTelemetrySubscriber(hub, incoming_qa_status_reader=reader)
        await subscriber._handle_incoming_qa_changed({"transaction_id": "77", "status": "UNTRUSTED"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [77]
        for socket in (first, second):
            message = socket.messages[-1]
            assert message["type"] == "incoming_qa_status"
            assert message["data"]["transaction"]["status"] == status
            assert message["data"]["transaction"]["request_id"] == "DB-AUTHORITY"
            assert message["data"]["job_gate_state"] == (
                "RELEASED" if status == "COMPLETED" else "NOT_RELEASED"
            )
            assert message["sequence"] == 2

    asyncio.run(run())


def test_reconnect_snapshot_contains_current_incoming_qa_projection() -> None:
    async def run() -> None:
        snapshot = {
            "jobs": [{"job_id": 19, "status": "RUNNING"}],
            "robots": [],
            "transports": [],
            "incoming_qa": [{
                "job_id": 19,
                "gate": {"status": "HOLD", "total_expected_items": 7, "released_items": 6},
                "transactions": [{"transaction_id": 77, "request_id": "REQ-2", "mode": "HOUSE_B", "cycle": 2, "status": "SENT", "retry_count": 1, "overall_result": None, "production_valid": None, "error_reason": None}],
                "items": [{"delivery_item_id": 31, "slot_id": "B02", "expected_part_code": "DB-B02", "status": "REQUESTED", "result": None, "failure_type": None}],
            }],
            "production_inspections": [],
            "active_errors": [],
        }
        socket = FakeWebSocket()
        await UnityRealtimeHub(lambda: snapshot).connect(socket)
        assert len(socket.messages) == 1
        assert socket.messages[0]["type"] == "production_snapshot"
        assert socket.messages[0]["sequence"] == 1
        assert socket.messages[0]["data"] == snapshot

    asyncio.run(run())


def test_incoming_qa_publisher_redis_failure_is_best_effort() -> None:
    class BrokenClient:
        async def publish_json(self, channel: str, payload: dict) -> int:
            assert channel == INCOMING_QA_CHANGED_CHANNEL
            assert payload["transaction_id"] == 19
            raise RuntimeError("redis unavailable")

        async def close(self) -> None:
            return None

    async def run() -> None:
        publisher = IncomingQAChangedPublisher(client_factory=BrokenClient)
        await publisher.start()
        publisher.notify_after_commit(19)
        await asyncio.sleep(0)
        await publisher.stop()

    asyncio.run(run())



def _all_items_pass_result(
    *, request_id: str, item_ids: dict[str, int]
) -> IncomingQAResultV02:
    return IncomingQAResultV02(
        inspection_request_id=request_id,
        inspection_cycle=1,
        inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        result="PASS",
        production_valid=True,
        items=[
            IncomingQAResultItemV02(
                slot_id=slot,
                delivery_item_id=item_ids[slot],
                expected_part_code=part_code,
                expected_class_name=vision_class,
                expected_quantity=1,
                predicted_class_name=vision_class,
                material_confidence=0.99,
                detected_quantity=1,
                result="PASS",
            )
            for slot, part_code, vision_class in (
                ("B01", "UNITY-B01", "wall_ext_back_window"),
                ("B02", "UNITY-B02", "wall_ext_door"),
            )
        ],
        camera_source="GLOBAL_CAMERA",
        timestamp="2026-09-05T00:00:00Z",
        model_scope="test",
        model_version="test",
    )


def test_udp_final_pass_notifies_production_only_after_committed_gate_release() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    incoming: list[int] = []
    production: list[tuple[int, str | None]] = []
    try:
        with factory() as session:
            job, items = _seed(session)
            base = _create(
                session, job=job, request_id="UNITY-BASE-READY",
                mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
                items=[("C09", items["C09"], "base_house_b")],
            )
            base.status = IncomingQATransactionStatus.COMPLETED
            base.overall_result = MaterialInspectionResult.PASS
            base.production_valid = True
            base_inspection = _inspection(session, base, items["C09"])
            base_inspection.status = MaterialInspectionStatus.COMPLETED
            base_inspection.result = MaterialInspectionResult.PASS
            base_inspection.production_valid = True
            transaction = _create(
                session,
                job=job,
                request_id="UNITY-GATE-RELEASE",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=1,
                items=[
                    ("B01", items["B01"], "wall_ext_back_window"),
                    ("B02", items["B02"], "wall_ext_door"),
                ],
            )
            session.commit()
            job_id, transaction_id = job.job_id, transaction.transaction_id
            item_ids = {slot: item.delivery_item_id for slot, item in items.items()}
            assert not IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job_id)

        set_incoming_qa_change_callback(incoming.append)
        committed_states: list[IncomingQATransactionStatus] = []

        def production_callback(job_id: int, reason: str | None) -> None:
            production.append((job_id, reason))
            with factory() as callback_session:
                committed = callback_session.get(IncomingQATransaction, transaction_id)
                assert committed is not None
                committed_states.append(committed.status)

        set_production_change_callback(production_callback)
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1", vision_port=9, result_host="127.0.0.1",
                result_port=0, ack_timeout_seconds=1, max_retries=0,
            ),
        )
        result = _all_items_pass_result(request_id="UNITY-GATE-RELEASE", item_ids=item_ids)
        assert runtime._apply_result_sync(result) == (True, transaction_id)
        assert incoming == [transaction_id]
        assert production == [(job_id, "incoming_qa_gate_released")]
        assert committed_states == [IncomingQATransactionStatus.COMPLETED]
        with factory() as session:
            assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job_id)
            assert session.get(IncomingQATransaction, transaction_id).status is IncomingQATransactionStatus.COMPLETED

        # The canonical duplicate is idempotent and cannot announce another gate transition.
        assert runtime._apply_result_sync(result) == (True, transaction_id)
        assert incoming == [transaction_id]
        assert production == [(job_id, "incoming_qa_gate_released")]
    finally:
        set_incoming_qa_change_callback(None)
        set_production_change_callback(None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_udp_final_result_production_callback_failure_and_invalid_result_do_not_corrupt_state() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    production: list[tuple[int, str | None]] = []
    try:
        with factory() as session:
            job, items = _seed(session)
            base = _create(
                session, job=job, request_id="UNITY-BASE-READY",
                mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
                items=[("C09", items["C09"], "base_house_b")],
            )
            base.status = IncomingQATransactionStatus.COMPLETED
            base.overall_result = MaterialInspectionResult.PASS
            base.production_valid = True
            base_inspection = _inspection(session, base, items["C09"])
            base_inspection.status = MaterialInspectionStatus.COMPLETED
            base_inspection.result = MaterialInspectionResult.PASS
            base_inspection.production_valid = True
            transaction = _create(
                session, job=job, request_id="UNITY-GATE-FAILURE",
                mode=IncomingQAInspectionMode.HOUSE_B, cycle=1,
                items=[
                    ("B01", items["B01"], "wall_ext_back_window"),
                    ("B02", items["B02"], "wall_ext_door"),
                ],
            )
            session.commit()
            job_id, transaction_id = job.job_id, transaction.transaction_id
            item_ids = {slot: item.delivery_item_id for slot, item in items.items()}

        def broken_callback(job_id: int, reason: str | None) -> None:
            production.append((job_id, reason))
            raise RuntimeError("publisher unavailable")

        set_production_change_callback(broken_callback)
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1", vision_port=9, result_host="127.0.0.1",
                result_port=0, ack_timeout_seconds=1, max_retries=0,
            ),
        )
        bad = _all_items_pass_result(request_id="UNITY-GATE-FAILURE", item_ids=item_ids)
        bad.items.pop()
        with pytest.raises(IncomingQACorrelationError):
            runtime._apply_result_sync(bad)
        assert production == []
        with factory() as session:
            assert session.get(IncomingQATransaction, transaction_id).status is IncomingQATransactionStatus.REQUESTED

        assert runtime._apply_result_sync(
            _all_items_pass_result(request_id="UNITY-GATE-FAILURE", item_ids=item_ids)
        ) == (True, transaction_id)
        assert production == [(job_id, "incoming_qa_gate_released")]
        with factory() as session:
            assert session.get(IncomingQATransaction, transaction_id).status is IncomingQATransactionStatus.COMPLETED
            assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job_id)
    finally:
        set_production_change_callback(None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_udp_runtime_notifies_only_committed_sent_acked_completed_and_rejected_states() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            job, items = _seed(session)
            completed = _create(
                session,
                job=job,
                request_id="UNITY-RUNTIME-COMPLETE",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=1,
                items=[("B01", items["B01"], "wall_ext_back_window")],
            )
            rejected = _create(
                session,
                job=job,
                request_id="UNITY-RUNTIME-REJECTED",
                mode=IncomingQAInspectionMode.BASE_AB,
                cycle=1,
                items=[("C09", items["C09"], "base_house_b")],
            )
            errored = _create(
                session,
                job=job,
                request_id="UNITY-RUNTIME-ERROR",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=2,
                items=[("B01", items["B01"], "wall_ext_back_window")],
            )
            session.commit()
            completed_id, rejected_id, error_id = (
                completed.transaction_id,
                rejected.transaction_id,
                errored.transaction_id,
            )
            b01_id = items["B01"].delivery_item_id

        observed: list[int] = []
        set_incoming_qa_change_callback(observed.append)
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1",
                vision_port=9,
                result_host="127.0.0.1",
                result_port=0,
                ack_timeout_seconds=1,
                max_retries=0,
            ),
        )
        runtime._prepare_send(completed_id, False)  # SENT
        runtime._apply_ack_sync(IncomingQAAckV02(
            inspection_request_id="UNITY-RUNTIME-COMPLETE",
            inspection_cycle=1,
            accepted=True,
            duplicate=False,
        ))
        runtime._apply_result_sync(IncomingQAResultV02(
            inspection_request_id="UNITY-RUNTIME-COMPLETE",
            inspection_cycle=1,
            inspection_mode=IncomingQAInspectionMode.HOUSE_B,
            result="PASS",
            production_valid=True,
            items=[IncomingQAResultItemV02(
                slot_id="B01",
                delivery_item_id=b01_id,
                expected_part_code="UNITY-B01",
                expected_class_name="wall_ext_back_window",
                expected_quantity=1,
                predicted_class_name="wall_ext_back_window",
                material_confidence=0.99,
                detected_quantity=1,
                result="PASS",
            )],
            camera_source="GLOBAL_CAMERA",
            timestamp="2026-09-05T00:00:00Z",
            model_scope="test",
            model_version="test",
        ))
        runtime._prepare_send(rejected_id, False)  # SENT
        runtime._apply_ack_sync(IncomingQAAckV02(
            inspection_request_id="UNITY-RUNTIME-REJECTED",
            inspection_cycle=1,
            accepted=False,
            duplicate=False,
            reason_code="CONTRACT_CONFLICT",
        ))
        runtime._prepare_send(error_id, False)  # SENT
        assert runtime._prepare_retry_or_error(error_id) is None  # max-retry ERROR
        assert observed == [
            completed_id, completed_id, completed_id,
            rejected_id, rejected_id,
            error_id, error_id,
        ]
        with factory() as session:
            assert session.get(IncomingQATransaction, completed_id).status is IncomingQATransactionStatus.COMPLETED
            assert session.get(IncomingQATransaction, rejected_id).status is IncomingQATransactionStatus.REJECTED
            assert session.get(IncomingQATransaction, error_id).status is IncomingQATransactionStatus.ERROR
    finally:
        set_incoming_qa_change_callback(None)
        Base.metadata.drop_all(engine)
        engine.dispose()



def test_initial_requested_transaction_notifies_only_after_orchestration_commit() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    observed: list[int] = []
    try:
        with factory() as session:
            seed_house_b_mvp_master(session)
            from scripts.seed_house_b_mvp_master import HOUSE_B_PARTS
            for part_code, _name, _vision_class in HOUSE_B_PARTS:
                session.add(Inventory(part_code=part_code, quantity=10))
            session.commit()
            job = ProductionOrchestrationService(session).create_job(
                product_code=HOUSE_B_PRODUCT_CODE,
                job_code="UNITY-REQUESTED-NOTIFICATION",
                roof_option_code=RoofOptionCode.ROOF_02,
            )
            set_incoming_qa_change_callback(observed.append)
            plan = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job.job_id)
            assert plan.send_transaction_id is not None
            transaction_id = plan.send_transaction_id
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            assert transaction is not None and transaction.status is IncomingQATransactionStatus.REQUESTED
        assert observed == [transaction_id]
    finally:
        set_incoming_qa_change_callback(None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_retry_count_commit_notifies_while_transaction_remains_sent() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    observed: list[int] = []
    try:
        with factory() as session:
            job, items = _seed(session)
            transaction = _create(
                session,
                job=job,
                request_id="UNITY-RUNTIME-RETRY",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=1,
                items=[("B01", items["B01"], "wall_ext_back_window")],
            )
            session.commit()
            transaction_id = transaction.transaction_id

        set_incoming_qa_change_callback(observed.append)
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1",
                vision_port=9,
                result_host="127.0.0.1",
                result_port=0,
                ack_timeout_seconds=1,
                max_retries=1,
            ),
        )
        runtime._prepare_send(transaction_id, False)
        assert runtime._prepare_retry_or_error(transaction_id) is not None

        with factory() as session:
            reloaded = session.get(IncomingQATransaction, transaction_id)
            assert reloaded is not None
            assert reloaded.status is IncomingQATransactionStatus.SENT
            assert reloaded.retry_count == 1
        assert observed == [transaction_id, transaction_id]
    finally:
        set_incoming_qa_change_callback(None)
        Base.metadata.drop_all(engine)
        engine.dispose()



def test_unity_job_gate_projection_reuses_generic_latest_effective_readiness() -> None:
    """The projection follows persisted delivery items, not a product/slot list."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def completed(
        session: Session,
        transaction: IncomingQATransaction,
        outcomes: list[tuple[JobMaterialDeliveryItem, MaterialInspectionResult]],
    ) -> None:
        transaction.status = IncomingQATransactionStatus.COMPLETED
        transaction.overall_result = (
            MaterialInspectionResult.PASS
            if all(result is MaterialInspectionResult.PASS for _, result in outcomes)
            else outcomes[0][1]
        )
        transaction.production_valid = transaction.overall_result is MaterialInspectionResult.PASS
        for item, result in outcomes:
            inspection = _inspection(session, transaction, item)
            inspection.status = MaterialInspectionStatus.COMPLETED
            inspection.result = result
            inspection.production_valid = result is MaterialInspectionResult.PASS

    try:
        with factory() as session:
            job, items = _seed(session)
            base = _create(
                session,
                job=job,
                request_id="UNITY-GATE-BASE",
                mode=IncomingQAInspectionMode.BASE_AB,
                cycle=1,
                items=[("C09", items["C09"], "base_house_b")],
            )
            completed(session, base, [(items["C09"], MaterialInspectionResult.PASS)])
            session.flush()
            projection = UnityIncomingQAProjectionService(session)

            # BASE PASS alone cannot release unrelated persisted expected items.
            assert projection.get_transaction_status(transaction_id=base.transaction_id)["job_gate_state"] == "NOT_RELEASED"

            house = _create(
                session,
                job=job,
                request_id="UNITY-GATE-HOUSE",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=1,
                items=[
                    ("B01", items["B01"], "wall_ext_back_window"),
                    ("B02", items["B02"], "wall_ext_door"),
                ],
            )
            completed(
                session,
                house,
                [
                    (items["B01"], MaterialInspectionResult.PASS),
                    (items["B02"], MaterialInspectionResult.PASS),
                ],
            )
            session.flush()
            assert projection.get_transaction_status(transaction_id=house.transaction_id)["job_gate_state"] == "RELEASED"

            b02 = _inspection(session, house, items["B02"])
            b02.result = MaterialInspectionResult.FAIL
            b02.production_valid = False
            house.overall_result = MaterialInspectionResult.FAIL
            house.production_valid = False
            session.flush()
            assert projection.get_transaction_status(transaction_id=house.transaction_id)["job_gate_state"] == "NOT_RELEASED"

            b02.result = MaterialInspectionResult.NOT_EVALUATED
            b02.production_valid = False
            house.overall_result = MaterialInspectionResult.NOT_EVALUATED
            session.flush()
            assert projection.get_transaction_status(transaction_id=house.transaction_id)["job_gate_state"] == "NOT_RELEASED"

            # An ERROR item remains unreleased even if earlier evidence was PASS.
            house.status = IncomingQATransactionStatus.ERROR
            house.overall_result = None
            b02.status = MaterialInspectionStatus.ERROR
            b02.result = None
            session.flush()
            assert projection.get_transaction_status(transaction_id=house.transaction_id)["job_gate_state"] == "NOT_RELEASED"

            reinspection = _create(
                session,
                job=job,
                request_id="UNITY-GATE-B02-CYCLE-2",
                mode=IncomingQAInspectionMode.HOUSE_B,
                cycle=2,
                items=[("B02", items["B02"], "wall_ext_door")],
            )
            completed(session, reinspection, [(items["B02"], MaterialInspectionResult.PASS)])
            session.flush()
            realtime = projection.get_transaction_status(transaction_id=reinspection.transaction_id)
            snapshot = projection.get_job_snapshot(job_id=job.job_id)
            assert realtime is not None and snapshot is not None
            assert realtime["job_gate_state"] == snapshot["job_gate_state"] == "RELEASED"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
