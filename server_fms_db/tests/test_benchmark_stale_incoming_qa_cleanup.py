from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.cleanup_benchmark_stale_incoming_qa import (
    BenchmarkIncomingQACleanupSafetyError,
    find_candidates,
    main,
    quarantine_candidate,
    require_benchmark_database_name,
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
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _stale_v02_item(session: Session, *, transaction_status: IncomingQATransactionStatus) -> MaterialInspection:
    product = Product(product_code="CLEANUP_PRODUCT", product_name="Cleanup fixture")
    part = Part(
        part_code="CLEANUP_BASE",
        part_name="Cleanup base",
        category=PartCategory.STRUCTURE,
        vision_class="base_house_b",
        unit="EA",
    )
    session.add_all([product, part])
    session.flush()
    job = ProductionJob(job_code="DEMO_CLEANUP_EXACT", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code="CLEANUP_DELIVERY",
        display_name="Cleanup delivery",
        status="PENDING",
    )
    session.add(delivery)
    session.flush()
    item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=part.part_code, quantity=1)
    session.add(item)
    session.flush()
    request_id = "cleanup-v02-request"
    transaction = IncomingQATransaction(
        inspection_request_id=request_id,
        production_job_id=job.job_id,
        inspection_mode="BASE_AB",
        inspection_cycle=1,
        status=transaction_status,
        immutable_request_snapshot="{}",
        error_reason="fixture terminal error" if transaction_status is IncomingQATransactionStatus.ERROR else None,
    )
    session.add(transaction)
    session.flush()
    inspection = MaterialInspection(
        inspection_request_id=request_id,
        incoming_qa_transaction_id=transaction.transaction_id,
        delivery_item_id=item.delivery_item_id,
        inspection_cycle=1,
        status=MaterialInspectionStatus.REQUESTED,
        expected_part_code=part.part_code,
        expected_class_name="base_house_b",
        expected_quantity=1,
    )
    session.add(inspection)
    session.commit()
    return inspection


def test_database_guard_rejects_production_and_accepts_benchmark() -> None:
    require_benchmark_database_name("smart_factory_benchmark")
    with pytest.raises(BenchmarkIncomingQACleanupSafetyError, match="smart_factory_db"):
        require_benchmark_database_name("smart_factory_db")


def test_apply_requires_exact_selector_before_opening_database() -> None:
    with pytest.raises(BenchmarkIncomingQACleanupSafetyError, match="exactly one explicit"):
        main(["--database-url", "postgresql://example.invalid/ignored", "--apply"])


def test_terminal_error_transaction_with_one_requested_item_is_quarantined_and_not_active(session: Session) -> None:
    inspection = _stale_v02_item(session, transaction_status=IncomingQATransactionStatus.ERROR)

    candidates = find_candidates(session, inspection_id=inspection.inspection_id)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.job_code == "DEMO_CLEANUP_EXACT"
    assert candidate.transaction_status is IncomingQATransactionStatus.ERROR

    assert quarantine_candidate(session, candidate, reason="fixture stale quarantine") == inspection.inspection_id

    persisted = session.get(MaterialInspection, inspection.inspection_id)
    assert persisted is not None
    assert persisted.status is MaterialInspectionStatus.ERROR
    assert persisted.result is None
    assert persisted.production_valid is None
    assert persisted.failure_reason == "fixture stale quarantine"
    assert persisted.completed_at is not None
    # The historical row and its v0.2 transaction remain; only the stuck item
    # becomes terminal and therefore leaves the existing global active predicate.
    assert session.get(IncomingQATransaction, persisted.incoming_qa_transaction_id) is not None
    assert IncomingQAOrchestrationService(session).get_active_inspection() is None


def test_active_transaction_is_refused_without_mutating_item(session: Session) -> None:
    inspection = _stale_v02_item(session, transaction_status=IncomingQATransactionStatus.SENT)
    candidate = find_candidates(session, inspection_id=inspection.inspection_id)[0]

    with pytest.raises(BenchmarkIncomingQACleanupSafetyError, match="already ERROR or REJECTED"):
        quarantine_candidate(session, candidate, reason="fixture stale quarantine")

    persisted = session.get(MaterialInspection, inspection.inspection_id)
    assert persisted is not None and persisted.status is MaterialInspectionStatus.REQUESTED
    assert IncomingQAOrchestrationService(session).get_active_inspection() is persisted


def test_exact_request_selector_does_not_mutate_unrelated_active_inspection(session: Session) -> None:
    first = _stale_v02_item(session, transaction_status=IncomingQATransactionStatus.ERROR)
    # A separate legacy request remains independently active; the targeted
    # quarantine must neither select nor mutate it.
    product = session.get(Product, "CLEANUP_PRODUCT")
    assert product is not None
    delivery = session.scalar(select(JobMaterialDelivery).where(JobMaterialDelivery.delivery_code == "CLEANUP_DELIVERY"))
    assert delivery is not None
    other_part = Part(part_code="CLEANUP_OTHER", part_name="Other", category=PartCategory.STRUCTURE, vision_class="wall_ext_door", unit="EA")
    session.add(other_part)
    session.flush()
    other_item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=other_part.part_code, quantity=1)
    session.add(other_item)
    session.flush()
    unrelated = MaterialInspection(
        inspection_request_id="unrelated-active-request",
        delivery_item_id=other_item.delivery_item_id,
        inspection_cycle=1,
        status=MaterialInspectionStatus.REQUESTED,
        expected_part_code=other_part.part_code,
        expected_class_name="wall_ext_door",
        expected_quantity=1,
    )
    session.add(unrelated)
    session.commit()

    candidate = find_candidates(session, request_id=first.inspection_request_id)[0]
    quarantine_candidate(session, candidate, reason="fixture stale quarantine")

    assert session.get(MaterialInspection, first.inspection_id).status is MaterialInspectionStatus.ERROR
    assert session.get(MaterialInspection, unrelated.inspection_id).status is MaterialInspectionStatus.REQUESTED
    assert IncomingQAOrchestrationService(session).get_active_inspection().inspection_id == unrelated.inspection_id
