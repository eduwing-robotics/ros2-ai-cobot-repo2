from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.worker import FmsWorker
from shared.models.factory import (
    Base,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.physical_ready_service import PhysicalReadyService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.transport_eligibility_service import (
    TransportEligibilityReason,
    TransportEligibilityService,
)


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="session")
def session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


def _make_delivery(
    session: Session,
    *,
    mode: SupplyMode | None = SupplyMode.TRANSPORTED,
    group: str | None = "OUTER_WALLS",
    ready: bool = False,
    qa: str = "INCOMPLETE",
    item_count: int = 1,
    suffix: str = "A",
) -> tuple[ProductionJob, JobMaterialDelivery]:
    product = session.get(Product, f"PHASE2_PRODUCT_{suffix}")
    if product is None:
        product = Product(product_code=f"PHASE2_PRODUCT_{suffix}", product_name=f"Phase 2 {suffix}")
        session.add(product)
        session.flush()
    job = ProductionJob(job_code=f"PHASE2_JOB_{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=f"PHASE2_DEL_{suffix}",
        display_name=f"Phase 2 delivery {suffix}",
        status=MaterialDeliveryStatus.PENDING,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED else None,
    )
    session.add(delivery)
    session.flush()
    for index in range(item_count):
        part = Part(
            part_code=f"PHASE2_PART_{suffix}_{index}",
            part_name=f"Phase 2 part {suffix} {index}",
            category=PartCategory.STRUCTURE,
            vision_class=f"test_class_{suffix}_{index}",
            unit="EA",
        )
        session.add(part)
        session.flush()
        step = JobStep(
            job_id=job.job_id,
            step_order=index + 1,
            operation_code="INSTALL_TEST",
            display_name=f"Install {index}",
            part_code=part.part_code,
            quantity=1,
            supply_mode=mode,
            supply_group_code=group,
            supply_destination_code="DROP" if mode is SupplyMode.TRANSPORTED else None,
            status=StepStatus.PENDING,
        )
        session.add(step)
        session.flush()
        item = JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id,
            job_step_id=step.job_step_id,
            part_code=part.part_code,
            quantity=1,
        )
        session.add(item)
        session.flush()
        if qa != "INCOMPLETE":
            inspection = MaterialInspection(
                inspection_request_id=f"phase2-{suffix}-{index}",
                delivery_item_id=item.delivery_item_id,
                inspection_cycle=1,
                status=MaterialInspectionStatus.COMPLETED if qa != "ERROR" else MaterialInspectionStatus.ERROR,
                result=MaterialInspectionResult.PASS if qa == "PASS" else (MaterialInspectionResult.FAIL if qa == "FAIL" else None),
                expected_part_code=part.part_code,
                expected_class_name=part.vision_class,
                expected_quantity=1,
                detected_quantity=1,
                production_valid=(qa == "PASS"),
            )
            session.add(inspection)
    session.commit()
    if ready:
        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=delivery.job_delivery_id,
            request_id=f"phase2-ready-{suffix}",
        )
    return job, delivery


def _payload(delivery: JobMaterialDelivery) -> dict:
    return {
        "job_id": delivery.production_job_id,
        "delivery_id": delivery.job_delivery_id,
        "pickup_code": "RACK1",
        "dropoff_code": "DROP",
    }


class ClaimInspectingForkliftTransport(FakeForkliftActionTransport):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__()
        self._session_factory = session_factory

    def send_execute_transport(self, **kwargs):
        with self._session_factory() as inspect_session:
            delivery = inspect_session.get(JobMaterialDelivery, kwargs["delivery_id"])
            attempt = inspect_session.scalar(
                select(ExecutionAttempt).where(ExecutionAttempt.req_id == kwargs["req_id"])
            )
            assert delivery is not None and delivery.status is MaterialDeliveryStatus.IN_PROGRESS
            assert attempt is not None
        return super().send_execute_transport(**kwargs)


def _worker(session_factory: sessionmaker[Session], transport: FakeForkliftActionTransport) -> FmsWorker:
    cell_transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(10)])
    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(transport)

    def fms_factory(session: Session) -> FmsExecutionCoordinator:
        return FmsExecutionCoordinator(
            session,
            orchestration_service=ProductionOrchestrationService(session),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
        )

    def forklift_factory(session: Session) -> ForkliftExecutionCoordinator:
        return ForkliftExecutionCoordinator(
            session,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
        )

    def feed_factory(session: Session) -> MaterialFeedExecutionCoordinator:
        return MaterialFeedExecutionCoordinator(session, robot_cell_adapter=cell_adapter)

    return FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_factory,
        forklift_execution_coordinator_factory=forklift_factory,
        material_feed_execution_coordinator_factory=feed_factory,
    )


@pytest.mark.parametrize(
    ("qa", "ready", "reason"),
    [
        ("INCOMPLETE", False, TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE),
        ("PASS", False, TransportEligibilityReason.PHYSICAL_READY_REQUIRED),
        ("INCOMPLETE", True, TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE),
        ("FAIL", True, TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE),
        ("ERROR", True, TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE),
    ],
)
def test_policy_transport_requires_both_qa_and_physical_ready(
    session: Session,
    qa: str,
    ready: bool,
    reason: TransportEligibilityReason,
) -> None:
    _, delivery = _make_delivery(session, qa=qa, ready=ready, suffix=f"G{qa}{ready}")
    result = TransportEligibilityService(session).evaluate_transport_eligibility(
        job_delivery_id=delivery.job_delivery_id
    )
    assert result.eligible is False
    assert result.reason is reason


def test_policy_transport_claims_only_when_every_item_is_released(session: Session) -> None:
    _, delivery = _make_delivery(session, qa="PASS", ready=True, item_count=2, suffix="ALLPASS")
    service = TransportEligibilityService(session)
    evaluated = service.evaluate_transport_eligibility(job_delivery_id=delivery.job_delivery_id)
    assert evaluated.eligible is True
    claim = service.claim_transport(job_delivery_id=delivery.job_delivery_id, request_payload=_payload(delivery))
    assert claim.eligible is True and claim.request_id is not None
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.IN_PROGRESS
    attempt = session.get(ExecutionAttempt, claim.attempt_id)
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.CREATED
    assert service.evaluate_transport_eligibility(job_delivery_id=delivery.job_delivery_id).reason is TransportEligibilityReason.DELIVERY_NOT_PENDING


def test_empty_items_active_attempt_and_invalid_policy_fail_closed(session: Session) -> None:
    _, empty = _make_delivery(session, qa="INCOMPLETE", ready=True, item_count=0, suffix="EMPTY")
    service = TransportEligibilityService(session)
    assert service.evaluate_transport_eligibility(job_delivery_id=empty.job_delivery_id).reason is TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE

    _, active = _make_delivery(session, qa="PASS", ready=True, suffix="ACTIVE")
    session.add(
        ExecutionAttempt(
            req_id="phase2-active-attempt",
            executor_type="FORKLIFT",
            command_type="EXECUTE_TRANSPORT",
            job_id=active.production_job_id,
            job_delivery_id=active.job_delivery_id,
            attempt_no=1,
            status=ExecutionAttemptStatus.ACCEPTED,
            request_payload_json="{}",
        )
    )
    session.commit()
    assert service.evaluate_transport_eligibility(job_delivery_id=active.job_delivery_id).reason is TransportEligibilityReason.ACTIVE_TRANSPORT_ATTEMPT

    _, invalid = _make_delivery(session, mode=SupplyMode.TRANSPORTED, group="", qa="PASS", ready=False, suffix="INVALID")
    assert service.evaluate_transport_eligibility(job_delivery_id=invalid.job_delivery_id).reason is TransportEligibilityReason.POLICY_INVALID


def test_manual_and_legacy_are_explicitly_distinguished(session: Session) -> None:
    _, manual = _make_delivery(session, mode=SupplyMode.MANUAL, group="TEST_MANUAL", qa="PASS", ready=False, suffix="MANUAL")
    _, legacy = _make_delivery(session, mode=None, group=None, qa="INCOMPLETE", ready=False, suffix="LEGACY")
    service = TransportEligibilityService(session)
    assert service.evaluate_transport_eligibility(job_delivery_id=manual.job_delivery_id).reason is TransportEligibilityReason.MANUAL_TRANSPORT_NOT_APPLICABLE
    assert service.evaluate_transport_eligibility(job_delivery_id=legacy.job_delivery_id).reason is TransportEligibilityReason.LEGACY_PATH


def test_worker_dispatches_only_claimed_policy_transport_once(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _, delivery = _make_delivery(session, qa="PASS", ready=True, suffix="WORKER")
    transport = FakeForkliftActionTransport()
    worker = _worker(session_factory, transport)
    assert worker.tick() is True
    assert len(transport.execute_transport_requests) == 1
    assert transport.execute_transport_requests[0] == {
        "req_id": transport.execute_transport_requests[0]["req_id"],
        "job_id": delivery.production_job_id,
        "delivery_id": delivery.job_delivery_id,
        "pickup_code": "RACK1",
        "dropoff_code": "DROP",
    }
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.COMPLETED
    assert worker.tick() is False
    assert len(transport.execute_transport_requests) == 1


def test_worker_forwards_inner_wall_logical_codes_to_fake_adapter(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _, delivery = _make_delivery(
        session, group="INNER_WALL", qa="PASS", ready=True, suffix="INNERWORKER"
    )
    transport = FakeForkliftActionTransport()
    assert _worker(session_factory, transport).tick() is True
    assert transport.execute_transport_requests == [{
        "req_id": transport.execute_transport_requests[0]["req_id"],
        "job_id": delivery.production_job_id,
        "delivery_id": delivery.job_delivery_id,
        "pickup_code": "RACK2",
        "dropoff_code": "DROP",
    }]


def test_worker_rejects_unknown_policy_group_before_transport_claim(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _, delivery = _make_delivery(
        session, group="UNSUPPORTED_GROUP", qa="PASS", ready=True, suffix="UNKNOWNWORKER"
    )
    transport = FakeForkliftActionTransport()
    assert _worker(session_factory, transport).tick() is False
    assert transport.execute_transport_requests == []
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.PENDING


def test_worker_action_is_sent_only_after_durable_claim(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _make_delivery(session, qa="PASS", ready=True, suffix="CLAIMORDER")
    transport = ClaimInspectingForkliftTransport(session_factory)
    assert _worker(session_factory, transport).tick() is True
    assert len(transport.execute_transport_requests) == 1


def test_worker_blocks_incomplete_manual_and_preserves_legacy_dispatch(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _, incomplete = _make_delivery(session, qa="INCOMPLETE", ready=True, suffix="BLOCK")
    _, manual = _make_delivery(session, mode=SupplyMode.MANUAL, group="TEST_MANUAL", qa="PASS", ready=False, suffix="MANUALWORKER")
    _, legacy = _make_delivery(session, mode=None, group=None, qa="INCOMPLETE", ready=False, suffix="LEGACYWORKER")
    transport = FakeForkliftActionTransport()
    worker = _worker(session_factory, transport)
    # The product-wide expected set is fail-closed: a legacy row cannot bypass
    # missing Incoming-QA evidence.
    assert worker.tick() is False
    assert transport.execute_transport_requests == []
    session.refresh(incomplete)
    session.refresh(manual)
    assert incomplete.status is MaterialDeliveryStatus.PENDING
    assert manual.status is MaterialDeliveryStatus.PENDING
    session.refresh(legacy)
    assert legacy.status is MaterialDeliveryStatus.PENDING


def test_multi_group_eligibility_is_independent(session: Session) -> None:
    _, first = _make_delivery(session, qa="PASS", ready=True, suffix="GROUPA")
    _, second = _make_delivery(session, qa="INCOMPLETE", ready=True, suffix="GROUPB")
    service = TransportEligibilityService(session)
    assert service.evaluate_transport_eligibility(job_delivery_id=first.job_delivery_id).eligible is True
    assert service.evaluate_transport_eligibility(job_delivery_id=second.job_delivery_id).reason is TransportEligibilityReason.PRE_PRODUCTION_QA_INCOMPLETE


def test_failed_fake_transport_marks_claimed_delivery_and_attempt_failed(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    _, delivery = _make_delivery(session, qa="PASS", ready=True, suffix="FAILED")
    transport = FakeForkliftActionTransport(
        execute_transport_result=ForkliftExecutionResult(
            status=ForkliftActionStatus.FAILED,
            error_code="FAKE_FAILURE",
            detail="planned fake transport failure",
        )
    )
    worker = _worker(session_factory, transport)
    assert worker.tick() is True
    session.refresh(delivery)
    assert delivery.status is MaterialDeliveryStatus.FAILED
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.job_delivery_id == delivery.job_delivery_id))
    assert attempt is not None and attempt.status is ExecutionAttemptStatus.FAILED
    assert delivery.physical_ready_at is not None


def test_shared_drop_blocks_second_eligible_policy_delivery(session: Session) -> None:
    _, outer = _make_delivery(session, group="OUTER_WALLS", qa="PASS", ready=True, suffix="DROP_OUTER")
    _, inner = _make_delivery(session, group="INNER_WALL", qa="PASS", ready=True, suffix="DROP_INNER")
    service = TransportEligibilityService(session)

    first = service.claim_transport(
        job_delivery_id=outer.job_delivery_id, request_payload=_payload(outer)
    )
    second = service.claim_transport(
        job_delivery_id=inner.job_delivery_id,
        request_payload={
            "job_id": inner.production_job_id,
            "delivery_id": inner.job_delivery_id,
            "pickup_code": "RACK2",
            "dropoff_code": "DROP",
        },
    )

    assert first.eligible is True
    assert second.eligible is False
    assert second.reason is TransportEligibilityReason.DROP_RESOURCE_OCCUPIED
    session.refresh(outer)
    session.refresh(inner)
    assert outer.status is MaterialDeliveryStatus.IN_PROGRESS
    assert inner.status is MaterialDeliveryStatus.PENDING
    attempts = list(session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.command_type == "EXECUTE_TRANSPORT"
    )))
    assert len(attempts) == 1
