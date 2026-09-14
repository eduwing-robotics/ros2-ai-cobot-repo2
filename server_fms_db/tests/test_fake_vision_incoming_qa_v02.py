from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.incoming_material_qa_udp_transport import IncomingQAUdpRuntime, IncomingQAUdpRuntimeConfig
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02OrchestrationService
from scripts.fake_vision_incoming_qa_v02 import (
    FakeVisionConfigurationError,
    FakeVisionIncomingQAConfig,
    FakeVisionIncomingQASimulator,
    FakeVisionScenario,
    _scenario_result,
)
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import IncomingQARequestItemV02, IncomingQARequestV02
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_v02_monitoring_service import IncomingQAV02MonitoringService
from shared.services.incoming_qa_transaction_service import IncomingQATransactionService
from shared.vision_recipe_mapping import IncomingQAInspectionMode


_HOUSE_B = (
    ("base_house_b", "BASE-B"),
    ("wall_ext_back_window", "B01-P"),
    ("wall_ext_door", "B02-P"),
    ("wall_ext_left_window", "B03-P"),
    ("wall_ext_right", "B04-P"),
    ("wall_int_house_b", "B05-P"),
    ("roof_zip", "B06-P"),
)


class _DatagramQueue(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.put_nowait((data, addr))


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        return int(reserve.getsockname()[1])


@pytest.fixture
def house_b_context(tmp_path) -> Iterator[tuple[sessionmaker[Session], int]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'fake_vision_house_b.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        session.add(Product(product_code="HOUSE_B", product_name="House B", is_active=True))
        session.flush()
        job = ProductionJob(job_code="FAKE-VISION-HOUSE-B", product_code="HOUSE_B", status=JobStatus.REQUESTED)
        session.add(job)
        session.flush()
        delivery = JobMaterialDelivery(
            production_job_id=job.job_id, batch_order=1, delivery_code="FAKE-VISION",
            display_name="Fake Vision", status="PENDING",
        )
        session.add(delivery)
        session.flush()
        session.add_all([
            Part(part_code=part_code, part_name=part_code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA")
            for vision_class, part_code in _HOUSE_B
        ])
        session.flush()
        session.add_all([
            JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=part_code, quantity=1)
            for _vision_class, part_code in _HOUSE_B
        ])
        session.commit()
        yield factory, job.job_id
    engine.dispose()


async def _wait_for_status(
    factory: sessionmaker[Session], transaction_id: int, status: IncomingQATransactionStatus
) -> None:
    for _ in range(150):
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            if transaction is not None and transaction.status is status:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"transaction={transaction_id} did not become {status.value}")


def _transactions(session: Session, job_id: int) -> list[IncomingQATransaction]:
    return list(session.scalars(
        select(IncomingQATransaction)
        .where(IncomingQATransaction.production_job_id == job_id)
        .order_by(IncomingQATransaction.transaction_id)
    ))


def test_real_local_udp_base_then_house_b_all_pass_releases_existing_gate(house_b_context) -> None:
    factory, job_id = house_b_context

    async def exercise() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        result_port = _free_udp_port()
        fake = FakeVisionIncomingQASimulator(FakeVisionIncomingQAConfig(
            host="127.0.0.1", request_port=0,
            server_result_host="127.0.0.1", server_result_port=result_port,
            scenario=FakeVisionScenario.ALL_PASS, result_delay_seconds=0.03,
        ))
        await fake.start()
        assert fake.bound_port is not None
        runtime = IncomingQAUdpRuntime(
            session_factory=factory,
            config=IncomingQAUdpRuntimeConfig(
                vision_host="127.0.0.1", vision_port=fake.bound_port,
                result_host="127.0.0.1", result_port=result_port,
                ack_timeout_seconds=0.1, max_retries=1,
            ),
        )
        await runtime.start()
        try:
            with factory() as session:
                initial = IncomingQAV02OrchestrationService(session).start_initial_inspection(job_id=job_id)
                assert initial.send_transaction_id is not None
                base_id = initial.send_transaction_id
            await runtime.send_transaction(base_id)
            await _wait_for_status(factory, base_id, IncomingQATransactionStatus.COMPLETED)

            with factory() as session:
                followup = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
                assert followup.send_transaction_id is not None
                house_id = followup.send_transaction_id
            await runtime.send_transaction(house_id)
            await _wait_for_status(factory, house_id, IncomingQATransactionStatus.COMPLETED)
            return fake.ack_destinations, fake.result_destinations
        finally:
            await runtime.close()
            await fake.close()

    ack_destinations, result_destinations = asyncio.run(exercise())
    with factory() as session:
        transactions = _transactions(session, job_id)
        assert [(tx.inspection_mode, tx.inspection_cycle, tx.status) for tx in transactions] == [
            ("BASE_AB", 1, IncomingQATransactionStatus.COMPLETED),
            ("HOUSE_B", 1, IncomingQATransactionStatus.COMPLETED),
        ]
        inspections = list(session.scalars(select(MaterialInspection).order_by(MaterialInspection.delivery_item_id)))
        assert len(inspections) == 7
        assert IncomingQAOrchestrationService(session).is_preproduction_ready(job_id=job_id)
        monitor = IncomingQAV02MonitoringService(session).get_job_monitor(job_id=job_id)
        assert monitor.gate.status == "RELEASE"
        assert [(transaction.inspection_mode, transaction.status.value) for transaction in monitor.transactions] == [
            ("BASE_AB", "COMPLETED"),
            ("HOUSE_B", "COMPLETED"),
        ]

    # ACK returns to the runtime sender's ephemeral source port; final results
    # go to the separately configured fixed listener port.
    assert len(ack_destinations) == len(result_destinations) == 2
    assert all(destination[0] == "127.0.0.1" for destination in ack_destinations)
    assert all(destination[1] == result_destinations[0][1] for destination in result_destinations)
    assert all(destination[1] != result_destinations[0][1] for destination in ack_destinations)


def test_real_local_udp_drop_first_ack_retries_same_sender_port_and_completes(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fake_vision_retry.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with factory() as session:
            session.add_all([
                Product(product_code="FAKE_B", product_name="Fake", is_active=True),
                Part(part_code="B02-RETRY", part_name="B02", category=PartCategory.STRUCTURE, vision_class="wall_ext_door", unit="EA"),
            ])
            session.flush()
            job = ProductionJob(job_code="FAKE-RETRY", product_code="FAKE_B", status=JobStatus.REQUESTED)
            session.add(job)
            session.flush()
            delivery = JobMaterialDelivery(production_job_id=job.job_id, batch_order=1, delivery_code="R", display_name="R", status="PENDING")
            session.add(delivery)
            session.flush()
            item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code="B02-RETRY", quantity=1)
            session.add(item)
            session.flush()
            request = IncomingQARequestV02(
                inspection_request_id="FAKE-RETRY-1", inspection_cycle=1,
                inspection_mode=IncomingQAInspectionMode.HOUSE_B,
                items=[IncomingQARequestItemV02(slot_id="B02", delivery_item_id=item.delivery_item_id, expected_part_code="B02-RETRY", expected_class_name="wall_ext_door", expected_quantity=1)],
            )
            transaction_id = IncomingQATransactionService().create_or_get(session, production_job_id=job.job_id, request=request).transaction.transaction_id
            session.commit()

        async def exercise() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
            result_port = _free_udp_port()
            fake = FakeVisionIncomingQASimulator(FakeVisionIncomingQAConfig(
                host="127.0.0.1", request_port=0, server_result_host="127.0.0.1", server_result_port=result_port,
                drop_first_ack=True, result_delay_seconds=0.03,
            ))
            await fake.start()
            runtime = IncomingQAUdpRuntime(
                session_factory=factory,
                config=IncomingQAUdpRuntimeConfig(
                    vision_host="127.0.0.1", vision_port=fake.bound_port or 1,
                    result_host="127.0.0.1", result_port=result_port, ack_timeout_seconds=0.02, max_retries=1,
                ),
            )
            await runtime.start()
            try:
                await runtime.send_transaction(transaction_id)
                await _wait_for_status(factory, transaction_id, IncomingQATransactionStatus.COMPLETED)
                assert len(fake.requests) == 2
                assert fake.requests[0].source_port == fake.requests[1].source_port
                return fake.ack_destinations, fake.result_destinations
            finally:
                await runtime.close()
                await fake.close()

        ack_destinations, result_destinations = asyncio.run(exercise())
        with factory() as session:
            transaction = session.get(IncomingQATransaction, transaction_id)
            assert transaction is not None
            assert transaction.status is IncomingQATransactionStatus.COMPLETED
            assert transaction.retry_count == 1
            assert transaction.ack_accepted is True
            inspections = list(session.scalars(select(MaterialInspection).where(
                MaterialInspection.incoming_qa_transaction_id == transaction_id
            )))
            assert len(inspections) == 1
        assert len(ack_destinations) == len(result_destinations) == 1
    finally:
        engine.dispose()


def test_real_udp_duplicate_request_acks_source_port_and_results_fixed_port() -> None:
    async def exercise() -> None:
        result_port = _free_udp_port()
        fake = FakeVisionIncomingQASimulator(FakeVisionIncomingQAConfig(
            host="127.0.0.1", request_port=0, server_result_host="127.0.0.1", server_result_port=result_port,
        ))
        await fake.start()
        loop = asyncio.get_running_loop()
        results = _DatagramQueue()
        result_transport, _ = await loop.create_datagram_endpoint(lambda: results, local_addr=("127.0.0.1", result_port))
        acks = _DatagramQueue()
        sender, _ = await loop.create_datagram_endpoint(lambda: acks, local_addr=("127.0.0.1", 0))
        try:
            request = IncomingQARequestV02(
                inspection_request_id="FAKE-DUPLICATE", inspection_cycle=1, inspection_mode=IncomingQAInspectionMode.HOUSE_B,
                items=[IncomingQARequestItemV02(slot_id="B01", delivery_item_id=101, expected_part_code="B01", expected_class_name="wall_ext_back_window", expected_quantity=1)],
            ).model_dump_json().encode("utf-8")
            sender.sendto(request, ("127.0.0.1", fake.bound_port or 1))
            sender.sendto(request, ("127.0.0.1", fake.bound_port or 1))
            first_ack, _ = await asyncio.wait_for(acks.received.get(), timeout=1)
            second_ack, _ = await asyncio.wait_for(acks.received.get(), timeout=1)
            first_result, _ = await asyncio.wait_for(results.received.get(), timeout=1)
            second_result, _ = await asyncio.wait_for(results.received.get(), timeout=1)
            assert IncomingQATransactionStatus  # keeps status import meaningful in this raw wire test
            assert json_load(first_ack)["duplicate"] is False
            assert json_load(second_ack)["duplicate"] is True
            assert json_load(first_result)["message_type"] == "incoming_qa_result"
            assert json_load(second_result)["message_type"] == "incoming_qa_result"
            assert fake.ack_destinations[0][1] != result_port
            assert fake.result_destinations == [("127.0.0.1", result_port), ("127.0.0.1", result_port)]
        finally:
            sender.close()
            result_transport.close()
            await fake.close()

    asyncio.run(exercise())


def json_load(payload: bytes) -> dict[str, object]:
    import json

    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    "scenario,mode,expected",
    [
        (FakeVisionScenario.ALL_PASS, IncomingQAInspectionMode.BASE_AB, "PASS"),
        (FakeVisionScenario.BASE_FAIL, IncomingQAInspectionMode.BASE_AB, "FAIL"),
        (FakeVisionScenario.BASE_NOT_EVALUATED, IncomingQAInspectionMode.BASE_AB, "NOT_EVALUATED"),
        (FakeVisionScenario.B02_FAIL, IncomingQAInspectionMode.HOUSE_B, "FAIL"),
    ],
)
def test_scenarios_produce_valid_v02_results(scenario, mode, expected) -> None:
    slot = "C09" if mode is IncomingQAInspectionMode.BASE_AB else "B02"
    expected_class = "base_house_b" if slot == "C09" else "wall_ext_door"
    request = IncomingQARequestV02(
        inspection_request_id=f"SCENARIO-{scenario.value}", inspection_cycle=1, inspection_mode=mode,
        items=[IncomingQARequestItemV02(slot_id=slot, delivery_item_id=11, expected_part_code="P", expected_class_name=expected_class, expected_quantity=1)],
    )
    result = _scenario_result(request, scenario)
    assert result.result == expected
    if scenario is FakeVisionScenario.BASE_NOT_EVALUATED:
        assert result.items[0].failure_type is None


def test_b02_fail_scenario_preserves_frozen_item_wire_shape_and_transaction_aggregate() -> None:
    request = IncomingQARequestV02(
        inspection_request_id="SCENARIO-B02-MIXED", inspection_cycle=1, inspection_mode=IncomingQAInspectionMode.HOUSE_B,
        items=[
            IncomingQARequestItemV02(slot_id="B01", delivery_item_id=101, expected_part_code="P-B01", expected_class_name="wall_ext_back_window", expected_quantity=1),
            IncomingQARequestItemV02(slot_id="B02", delivery_item_id=102, expected_part_code="P-B02", expected_class_name="wall_ext_door", expected_quantity=1),
            IncomingQARequestItemV02(slot_id="B03", delivery_item_id=103, expected_part_code="P-B03", expected_class_name="wall_ext_left_window", expected_quantity=1),
        ],
    )
    result = _scenario_result(request, FakeVisionScenario.B02_FAIL)
    assert result.result == "FAIL" and result.production_valid is False
    assert {item.slot_id: item.result for item in result.items} == {
        "B01": "PASS", "B02": "FAIL", "B03": "PASS",
    }
    assert all("production_valid" not in item.model_dump() for item in result.items)


def test_malformed_udp_request_is_rejected_and_loopback_guard_is_fail_closed() -> None:
    with pytest.raises(FakeVisionConfigurationError, match="allow-non-loopback"):
        FakeVisionIncomingQAConfig(
            host="192.168.20.30", request_port=20051,
            server_result_host="127.0.0.1", server_result_port=20052,
        )

    async def exercise() -> None:
        fake = FakeVisionIncomingQASimulator(FakeVisionIncomingQAConfig(
            host="127.0.0.1", request_port=0, server_result_host="127.0.0.1", server_result_port=_free_udp_port(),
        ))
        await fake.start()
        loop = asyncio.get_running_loop()
        sender, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("127.0.0.1", fake.bound_port or 1))
        try:
            sender.sendto(b"not-json")
            await asyncio.sleep(0.02)
            assert fake.invalid_datagram_count == 1
        finally:
            sender.close()
            await fake.close()

    asyncio.run(exercise())
