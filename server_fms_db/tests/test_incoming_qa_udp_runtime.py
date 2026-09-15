from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_material_qa_udp_transport import (
    IncomingQACorrelationError,
    IncomingQAUdpConfigurationError,
    IncomingQAUdpRuntime,
    IncomingQAUdpRuntimeError,
    IncomingQAUdpRuntimeConfig,
)
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import (
    IncomingMaterialQAResult,
    IncomingQAAckV02,
    IncomingQARequestItemV02,
    IncomingQARequestV02,
    IncomingQAResultItemV02,
    IncomingQAResultV02,
)
from shared.services.incoming_qa_transaction_service import IncomingQATransactionService
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.vision_recipe_mapping import IncomingQAInspectionMode


class _QueueProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[no-untyped-def]
        self._queue.put_nowait(data)


@pytest.fixture
def seeded_transaction() -> Iterator[tuple[sessionmaker[Session], int, IncomingQARequestV02]]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        session.add(Product(product_code="UDP_TEST", product_name="UDP test", is_active=True))
        session.flush()
        job = ProductionJob(job_code="UDP-TEST-JOB", product_code="UDP_TEST", status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id,
            batch_order=1,
            delivery_code="UDP-TEST",
            display_name="UDP test",
            status="PENDING",
        )
        session.add(delivery)
        session.flush()
        session.add_all([
            Part(part_code="UDP-B01", part_name="B01", category=PartCategory.STRUCTURE, vision_class="wall_ext_back_window", unit="EA"),
            Part(part_code="UDP-B02", part_name="B02", category=PartCategory.STRUCTURE, vision_class="wall_ext_door", unit="EA"),
        ])
        session.flush()
        session.add_all([
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code="UDP-B01", quantity=1),
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code="UDP-B02", quantity=1),
        ])
        session.commit()
        items = list(session.scalars(select(JobMaterialDeliveryItem).order_by(JobMaterialDeliveryItem.delivery_item_id)))
        request = IncomingQARequestV02(
            inspection_request_id="REQ-UDP-100",
            inspection_cycle=1,
            inspection_mode=IncomingQAInspectionMode.HOUSE_B,
            items=[
                IncomingQARequestItemV02(slot_id="B01", delivery_item_id=items[0].delivery_item_id, expected_part_code="UDP-B01", expected_class_name="wall_ext_back_window", expected_quantity=1),
                IncomingQARequestItemV02(slot_id="B02", delivery_item_id=items[1].delivery_item_id, expected_part_code="UDP-B02", expected_class_name="wall_ext_door", expected_quantity=1),
            ],
        )
        created = IncomingQATransactionService().create_or_get(
            session, production_job_id=job.job_id, request=request
        )
        session.commit()
        yield factory, created.transaction.transaction_id, request
    engine.dispose()


def test_legacy_result_service_rejects_v02_transaction_managed_item(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        inspection = session.scalar(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        ))
        assert inspection is not None
        item = request.items[0]
        legacy_result = IncomingMaterialQAResult(
            inspection_request_id=request.inspection_request_id,
            delivery_item_id=item.delivery_item_id,
            inspection_cycle=request.inspection_cycle,
            status="COMPLETED",
            result="PASS",
            failure_type=None,
            expected_part_code=item.expected_part_code,
            expected_class_name=item.expected_class_name,
            expected_quantity=item.expected_quantity,
            detected_quantity=item.expected_quantity,
            detections=[],
            frame_width=640,
            frame_height=480,
            camera_source="GLOBAL_CAMERA",
            frame_seq=1,
            timestamp="2026-09-05T00:00:00Z",
            model_scope="legacy-test",
            model_version="v0.1",
            production_valid=True,
        )
        from shared.services.material_inspection_service import (
            MaterialInspectionCorrelationError,
            MaterialInspectionService,
        )
        with pytest.raises(MaterialInspectionCorrelationError, match="v0.2 transaction-managed"):
            MaterialInspectionService().apply_inspection_result(session, legacy_result)
        session.expire_all()
        stored = session.get(MaterialInspection, inspection.inspection_id)
        assert stored is not None and stored.status is MaterialInspectionStatus.REQUESTED


def _result(request: IncomingQARequestV02, *, overall: str = "PASS", valid: bool = True) -> IncomingQAResultV02:
    items = [
        IncomingQAResultItemV02(
            **item.model_dump(),
            predicted_class_name=item.expected_class_name,
            material_confidence=0.99,
            detected_quantity=item.expected_quantity,
            result="PASS",
            failure_type=None,
            defects=[],
            quality_scores={"surface": 0.99},
        )
        for item in request.items
    ]
    if overall != "PASS":
        items[1] = IncomingQAResultItemV02(
            **request.items[1].model_dump(),
            predicted_class_name="wall_ext_back_window",
            material_confidence=0.4,
            detected_quantity=0,
            result=overall,
            failure_type="MISSING" if overall == "FAIL" else None,
            defects=["COMPONENT_MISSING"] if overall == "FAIL" else [],
        )
    return IncomingQAResultV02(
        inspection_request_id=request.inspection_request_id,
        inspection_cycle=request.inspection_cycle,
        inspection_mode=request.inspection_mode,
        result=overall,
        production_valid=valid,
        items=items,
        camera_source="GLOBAL_CAMERA",
        timestamp="2026-09-04T01:02:03Z",
        model_scope="incoming-qa",
        model_version="v0.2-test",
    )


def _runtime(factory: sessionmaker[Session], *, vision_port: int, timeout: float = 0.05, retries: int = 1) -> IncomingQAUdpRuntime:
    return IncomingQAUdpRuntime(
        session_factory=factory,
        config=IncomingQAUdpRuntimeConfig(
            vision_host="127.0.0.1",
            vision_port=vision_port,
            result_host="127.0.0.1",
            result_port=0,
            ack_timeout_seconds=timeout,
            max_retries=retries,
        ),
    )


async def _wait_for_status(factory: sessionmaker[Session], transaction_id: int, expected: IncomingQATransactionStatus) -> None:
    for _ in range(100):
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            if transaction is not None and transaction.status is expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"transaction {transaction_id} did not reach {expected}")



async def _wait_for_retry_count(factory: sessionmaker[Session], transaction_id: int, expected: int) -> None:
    for _ in range(100):
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            if transaction is not None and transaction.retry_count == expected:
                return
        await asyncio.sleep(0.005)
    raise AssertionError(f"transaction {transaction_id} did not reach retry_count={expected}")


async def _wait_for_request_count(protocol: "_AckingVisionProtocol", expected: int) -> None:
    for _ in range(100):
        if len(protocol.requests) >= expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"Vision did not receive {expected} requests")

def test_explicit_udp_config_is_disabled_or_rejected_without_defaults() -> None:
    assert IncomingQAUdpRuntimeConfig.from_settings(object()) is None
    with pytest.raises(IncomingQAUdpConfigurationError):
        IncomingQAUdpRuntimeConfig(
            vision_host="", vision_port=1, result_host="127.0.0.1", result_port=2,
            ack_timeout_seconds=1, max_retries=0,
        )


def test_ack_correlation_rejects_unknown_and_wrong_cycle_without_mutation(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)

    async def exercise() -> None:
        assert not await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id="unknown", inspection_cycle=1, accepted=True
        ))
        assert not await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=2, accepted=True
        ))
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=1, accepted=True
        ))
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=1, accepted=True, duplicate=True
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.ACKED
        assert transaction.ack_accepted is True
        assert transaction.ack_duplicate is True


def test_rejected_ack_is_terminal_wait_block(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)

    async def exercise() -> None:
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id,
            inspection_cycle=1,
            accepted=False,
            reason_code="CONTRACT_CONFLICT",
        ))
        assert not await runtime.handle_result(_result(request))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.REJECTED
        assert transaction.ack_reason_code == "CONTRACT_CONFLICT"
        assert transaction.error_reason is None
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.ERROR}
        assert {inspection.failure_reason for inspection in inspections} == {"ACK_REJECTED:CONTRACT_CONFLICT"}
        assert IncomingQAOrchestrationService(session).get_active_inspection() is None


def test_final_result_is_atomic_idempotent_and_preserves_evidence(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)
    result = _result(request, overall="FAIL", valid=False)

    async def exercise() -> None:
        # UDP reordering: terminal evidence is allowed before its ACK.
        assert await runtime.handle_result(result)
        assert await runtime.handle_result(result)
        conflicting = _result(request, overall="PASS", valid=True)
        assert not await runtime.handle_result(conflicting)

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.COMPLETED
        assert transaction.overall_result.value == "FAIL"
        assert transaction.production_valid is False
        assert transaction.result_snapshot_json is not None
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(inspections) == 2
        assert all(item.status is MaterialInspectionStatus.COMPLETED for item in inspections)
        failed = next(item for item in inspections if item.result and item.result.value == "FAIL")
        passed = next(item for item in inspections if item.result and item.result.value == "PASS")
        assert failed.production_valid is False
        assert passed.production_valid is True
        assert failed.failure_type and failed.failure_type.value == "MISSING"
        assert json.loads(failed.result_detail_json or "{}")["defects"] == ["COMPONENT_MISSING"]


def test_result_first_then_matching_ack_persists_metadata_without_completed_rollback(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)

    async def exercise() -> None:
        # Explicit handler ordering is deterministic; no timing or thread race is
        # needed to reproduce RESULT commit before the independently scheduled ACK.
        assert await runtime.handle_result(_result(request))
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle,
            accepted=True,
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.COMPLETED
        assert transaction.ack_accepted is True
        assert transaction.ack_duplicate is False
        assert transaction.ack_reason_code is None
        assert transaction.acked_at is not None
        assert transaction.overall_result is not None
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(inspections) == 2
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.COMPLETED}


def test_completed_duplicate_ack_updates_diagnostics_without_mutating_result(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)

    async def exercise() -> None:
        assert await runtime.handle_result(_result(request, overall="FAIL", valid=False))
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle,
            accepted=True, duplicate=True,
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.COMPLETED
        assert transaction.overall_result is not None and transaction.overall_result.value == "FAIL"
        assert transaction.production_valid is False
        assert transaction.ack_accepted is True
        assert transaction.ack_duplicate is True
        assert transaction.acked_at is not None
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(inspections) == 2
        assert sum(inspection.result.value == "FAIL" for inspection in inspections if inspection.result) == 1


def test_completed_rejected_ack_records_diagnostics_without_rejection_rollback(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)

    async def exercise() -> None:
        assert await runtime.handle_result(_result(request))
        assert await runtime.handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle,
            accepted=False, reason_code="CONTRACT_CONFLICT",
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.COMPLETED
        assert transaction.ack_accepted is False
        assert transaction.ack_duplicate is False
        assert transaction.ack_reason_code == "CONTRACT_CONFLICT"
        assert transaction.acked_at is not None
        assert transaction.overall_result is not None and transaction.overall_result.value == "PASS"
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(inspections) == 2


def test_error_transaction_keeps_existing_late_ack_semantics(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        transaction.status = IncomingQATransactionStatus.ERROR
        transaction.error_reason = "ACK_TIMEOUT_MAX_RETRIES"
        session.commit()

    async def exercise() -> None:
        assert not await _runtime(factory, vision_port=9).handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle,
            accepted=True,
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.ERROR
        assert transaction.ack_accepted is None and transaction.acked_at is None


def test_invalid_result_item_is_atomic_noop(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)
    invalid_items = [item.model_copy() for item in _result(request).items]
    # Valid at schema level, but swaps durable item identity against immutable slot context.
    invalid_items[0] = invalid_items[0].model_copy(update={"delivery_item_id": request.items[1].delivery_item_id})
    invalid_items[1] = invalid_items[1].model_copy(update={"delivery_item_id": request.items[0].delivery_item_id})
    invalid = _result(request).model_copy(update={"items": invalid_items})

    async def exercise() -> None:
        with pytest.raises(IncomingQACorrelationError):
            await runtime.handle_result(invalid)

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None and transaction.status is IncomingQATransactionStatus.REQUESTED
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.REQUESTED}


def test_loopback_sender_listener_and_timeout_retry_are_same_transaction(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        vision_transport, _ = await loop.create_datagram_endpoint(
            lambda: _QueueProtocol(queue), local_addr=("127.0.0.1", 0)
        )
        vision_port = int(vision_transport.get_extra_info("socket").getsockname()[1])
        runtime = _runtime(factory, vision_port=vision_port, timeout=0.02, retries=1)
        await runtime.start()
        try:
            await runtime.send_transaction(transaction_id)
            first = await asyncio.wait_for(queue.get(), timeout=1)
            second = await asyncio.wait_for(queue.get(), timeout=1)
            assert first == second
            sent = IncomingQARequestV02.model_validate_json(first)
            assert sent.inspection_request_id == request.inspection_request_id
            assert sent.inspection_cycle == request.inspection_cycle
            # StaticPool shares one SQLite connection with the retry worker.
            # Do not open a competing read transaction while that worker owns
            # the commit; wait for its task to finish, then assert durable state
            # below using a fresh session.
            for _ in range(150):
                if transaction_id not in runtime._retry_tasks:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("timeout retry task did not finish")
        finally:
            await runtime.close()
            vision_transport.close()

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.retry_count == 1
        assert transaction.error_reason == "ACK_TIMEOUT_MAX_RETRIES"
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(inspections) == 2
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.ERROR}
        assert {inspection.failure_reason for inspection in inspections} == {"ACK_TIMEOUT_MAX_RETRIES"}
        assert IncomingQAOrchestrationService(session).get_active_inspection() is None


def test_corrupt_snapshot_error_terminalizes_items_in_the_same_session(seeded_transaction) -> None:
    factory, transaction_id, _request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        transaction.immutable_request_snapshot = "not-json"
        session.commit()

    with pytest.raises(IncomingQAUdpRuntimeError, match="snapshot is invalid"):
        runtime._prepare_send(transaction_id, False)

    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.ERROR
        assert transaction.error_reason == "IMMUTABLE_REQUEST_SNAPSHOT_INVALID"
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.ERROR}
        assert IncomingQAOrchestrationService(session).get_active_inspection() is None


def test_explicit_runtime_error_terminalizes_items_without_global_busy(seeded_transaction) -> None:
    factory, transaction_id, _request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        transaction.status = IncomingQATransactionStatus.SENT
        session.commit()

    runtime._mark_runtime_error(transaction_id, "UDP_SEND_FAILED")

    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.ERROR
        assert transaction.error_reason == "UDP_SEND_FAILED"
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert {inspection.status for inspection in inspections} == {MaterialInspectionStatus.ERROR}
        assert {inspection.failure_reason for inspection in inspections} == {"UDP_SEND_FAILED"}
        assert IncomingQAOrchestrationService(session).get_active_inspection() is None


def test_terminal_transaction_items_do_not_hide_an_unrelated_active_inspection(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction
    with factory() as session:
        delivery = session.scalar(select(JobMaterialDelivery))
        assert delivery is not None
        session.add(Part(
            part_code="UDP-B03", part_name="B03", category=PartCategory.STRUCTURE,
            vision_class="wall_ext_left_window", unit="EA",
        ))
        session.flush()
        item = JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id, part_code="UDP-B03", quantity=1
        )
        session.add(item)
        session.flush()
        unrelated = MaterialInspection(
            inspection_request_id="REQ-UDP-UNRELATED",
            delivery_item_id=item.delivery_item_id,
            inspection_cycle=1,
            status=MaterialInspectionStatus.REQUESTED,
            expected_part_code="UDP-B03",
            expected_class_name="wall_ext_left_window",
            expected_quantity=1,
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.inspection_id

    async def exercise() -> None:
        assert await _runtime(factory, vision_port=9).handle_ack(IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id,
            inspection_cycle=request.inspection_cycle,
            accepted=False,
            reason_code="ACTIVE_INSPECTION_EXISTS",
        ))

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None and transaction.status is IncomingQATransactionStatus.REJECTED
        active = IncomingQAOrchestrationService(session).get_active_inspection()
        assert active is not None and active.inspection_id == unrelated_id


def test_listener_rejects_malformed_packet_without_db_mutation(seeded_transaction) -> None:
    factory, transaction_id, _request = seeded_transaction

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        blackhole, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        port = int(blackhole.get_extra_info("socket").getsockname()[1])
        runtime = _runtime(factory, vision_port=port)
        await runtime.start()
        try:
            assert runtime.bound_port is not None
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"not-json", ("127.0.0.1", runtime.bound_port))
            sender.close()
            await asyncio.sleep(0.03)
        finally:
            await runtime.close()
            blackhole.close()

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None and transaction.status is IncomingQATransactionStatus.REQUESTED

def test_close_drains_inflight_database_worker_before_engine_teardown(seeded_transaction) -> None:
    factory, _transaction_id, request = seeded_transaction
    runtime = _runtime(factory, vision_port=9)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_result(_incoming_result: IncomingQAResultV02) -> tuple[bool, int | None]:
        started.set()
        release.wait()
        finished.set()
        return False, None

    runtime._apply_result_sync = blocking_result  # type: ignore[method-assign]

    async def exercise() -> None:
        runtime.schedule_result(_result(request))
        assert await asyncio.to_thread(started.wait, 1)
        close_task = asyncio.create_task(runtime.close())
        try:
            await asyncio.sleep(0)
            assert not close_task.done()
        finally:
            release.set()
        await close_task
        assert finished.is_set()
        assert not runtime._db_tasks

    asyncio.run(exercise())


class _AckingVisionProtocol(asyncio.DatagramProtocol):
    """Loopback Vision stub which returns ACKs to the request source address."""

    def __init__(self, *, acknowledge_on_request: int = 1) -> None:
        self._acknowledge_on_request = acknowledge_on_request
        self.transport: asyncio.DatagramTransport | None = None
        self.requests: list[IncomingQARequestV02] = []
        self.request_sources: list[tuple[str, int]] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        request = IncomingQARequestV02.model_validate_json(data)
        self.requests.append(request)
        self.request_sources.append(addr)
        if len(self.requests) < self._acknowledge_on_request:
            return
        assert self.transport is not None
        ack = IncomingQAAckV02(
            inspection_request_id=request.inspection_request_id,
            inspection_cycle=request.inspection_cycle,
            accepted=True,
            duplicate=len(self.requests) > 1,
        )
        self.transport.sendto(ack.model_dump_json().encode("utf-8"), addr)


def test_ack_returns_to_sender_source_port_and_result_uses_fixed_listener(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        vision_transport, vision = await loop.create_datagram_endpoint(
            lambda: _AckingVisionProtocol(), local_addr=("127.0.0.1", 0)
        )
        vision_port = int(vision_transport.get_extra_info("socket").getsockname()[1])
        runtime = _runtime(factory, vision_port=vision_port, timeout=0.05, retries=1)
        final_sender: asyncio.DatagramTransport | None = None
        await runtime.start()
        try:
            await runtime.send_transaction(transaction_id)
            await _wait_for_status(factory, transaction_id, IncomingQATransactionStatus.ACKED)
            assert len(vision.requests) == 1
            # ACK reached the sender socket, while final evidence remains on the
            # independently configured FMS listener port.
            assert runtime.bound_port is not None
            assert vision.request_sources[0][1] != runtime.bound_port

            final_sender, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=("127.0.0.1", runtime.bound_port),
            )
            final_sender.sendto(_result(request).model_dump_json().encode("utf-8"))
            await _wait_for_status(factory, transaction_id, IncomingQATransactionStatus.COMPLETED)
        finally:
            if final_sender is not None:
                final_sender.close()
            await runtime.close()
            vision_transport.close()

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.COMPLETED
        assert transaction.ack_accepted is True
        assert transaction.acked_at is not None
        assert transaction.retry_count == 0


def test_retry_reuses_sender_source_port_then_accepts_returned_ack(seeded_transaction) -> None:
    factory, transaction_id, request = seeded_transaction

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        vision_transport, vision = await loop.create_datagram_endpoint(
            # Drop both automatic ACKs; the test returns one only after the
            # retry increment is durably observable.
            lambda: _AckingVisionProtocol(acknowledge_on_request=999),
            local_addr=("127.0.0.1", 0),
        )
        vision_port = int(vision_transport.get_extra_info("socket").getsockname()[1])
        runtime = _runtime(factory, vision_port=vision_port, timeout=0.1, retries=1)
        await runtime.start()
        try:
            await runtime.send_transaction(transaction_id)
            await _wait_for_request_count(vision, 2)
            await _wait_for_retry_count(factory, transaction_id, 1)
            assert vision.request_sources[0] == vision.request_sources[1]
            assert vision.transport is not None
            vision.transport.sendto(
                IncomingQAAckV02(
                    inspection_request_id=request.inspection_request_id,
                    inspection_cycle=request.inspection_cycle,
                    accepted=True,
                    duplicate=True,
                ).model_dump_json().encode("utf-8"),
                vision.request_sources[1],
            )
            await _wait_for_status(factory, transaction_id, IncomingQATransactionStatus.ACKED)
            await asyncio.sleep(0.12)
        finally:
            await runtime.close()
            vision_transport.close()

    asyncio.run(exercise())
    with factory() as session:
        transaction = session.get(IncomingQATransaction, transaction_id)
        assert transaction is not None
        assert transaction.status is IncomingQATransactionStatus.ACKED
        assert transaction.retry_count == 1
        assert transaction.error_reason is None
        transactions = list(session.scalars(select(IncomingQATransaction)))
        inspections = list(session.scalars(select(MaterialInspection).where(
            MaterialInspection.incoming_qa_transaction_id == transaction_id
        )))
        assert len(transactions) == 1
        assert len(inspections) == 2
