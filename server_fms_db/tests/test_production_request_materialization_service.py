from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from tests.recipe_test_support import add_active_recipe
from shared.models.factory import (
    EventType,
    JobStep,
    PendingProductionRequest,
    PendingProductionState,
    Product,
    ProductionEvent,
    ProductionJob,
    AssemblyRecipe,
    RoofOptionCode,
)
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.realtime.production_events import set_production_change_callback
from shared.services.production_request_materialization_service import (
    PendingMaterializationInvariantError,
    PendingMaterializationStateError,
    ProductionRequestMaterializationService,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    add_active_recipe(db, "HOUSE_A")
    add_active_recipe(db, "HOUSE_B")
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)


def create_pending(session: Session, now: datetime, *, quantity: int, state: PendingProductionState = PendingProductionState.AWAITING_CONFIRMATION) -> PendingProductionRequest:
    pending = PendingProductionRequestService(session, clock=lambda: now).create_pending(
        session_id=f"pending-{quantity}-{state.value}", product_code="HOUSE_B", quantity=quantity, roof_option_code=RoofOptionCode.ROOF_02
    )
    if state is PendingProductionState.CONFIRMED:
        pending = PendingProductionRequestService(session, clock=lambda: now).confirm_pending(request_id=pending.request_id)
    return pending


def materializer(session: Session, now: datetime) -> ProductionRequestMaterializationService:
    return ProductionRequestMaterializationService(session, clock=lambda: now)


def linked_jobs(session: Session, pending_id: int) -> list[ProductionJob]:
    return list(session.scalars(select(ProductionJob).where(ProductionJob.source_pending_request_id == pending_id).order_by(ProductionJob.source_item_index)))


def test_quantity_one_materializes_one_job_steps_and_event(session: Session, now: datetime) -> None:
    pending = create_pending(session, now, quantity=1)
    result = materializer(session, now).confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert result.created and result.pending.state is PendingProductionState.CONFIRMED
    assert [job.source_item_index for job in result.jobs] == [1]
    job = result.jobs[0]
    assert job.roof_option_code is RoofOptionCode.ROOF_02 and job.product.product_code == "HOUSE_B"
    assert len(job.steps) == 16
    assert session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id == job.job_id, ProductionEvent.event_type == EventType.JOB_CREATED)) == 1


def test_quantity_two_and_five_materialize_independent_unique_jobs(session: Session, now: datetime) -> None:
    two = create_pending(session, now, quantity=2)
    two_result = materializer(session, now).confirm_and_create_jobs(pending_request_id=two.request_id)
    assert len(two_result.jobs) == 2
    assert [job.source_item_index for job in two_result.jobs] == [1, 2]
    assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id.in_([job.job_id for job in two_result.jobs]))) == 32

    five = create_pending(session, now, quantity=5)
    five_result = materializer(session, now).confirm_and_create_jobs(pending_request_id=five.request_id)
    assert len({job.job_id for job in five_result.jobs}) == 5
    assert len({job.job_code for job in five_result.jobs}) == 5
    assert [job.source_item_index for job in five_result.jobs] == [1, 2, 3, 4, 5]


def test_materialization_is_idempotent(session: Session, now: datetime) -> None:
    pending = create_pending(session, now, quantity=2)
    service = materializer(session, now)
    first = service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    second = service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert first.created is True and second.created is False
    assert [job.job_id for job in second.jobs] == [job.job_id for job in first.jobs]
    assert len(linked_jobs(session, pending.request_id)) == 2
    assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id.in_([job.job_id for job in first.jobs]))) == 32
    assert session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id.in_([job.job_id for job in first.jobs]), ProductionEvent.event_type == EventType.JOB_CREATED)) == 2


def test_phase_four_confirmed_without_jobs_is_materialized_once(session: Session, now: datetime) -> None:
    pending = create_pending(session, now, quantity=2, state=PendingProductionState.CONFIRMED)
    service = materializer(session, now)
    first = service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    second = service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert len(first.jobs) == len(second.jobs) == 2
    assert first.created and not second.created


def test_failure_during_second_job_rolls_back_everything(session: Session, now: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = create_pending(session, now, quantity=2)
    service = materializer(session, now)
    original = service._create_job
    calls = {"count": 0}

    def fail_second(**kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("forced second job failure")
        return original(**kwargs)

    monkeypatch.setattr(service, "_create_job", fail_second)
    with pytest.raises(RuntimeError, match="forced second job failure"):
        service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    stored = session.get(PendingProductionRequest, pending.request_id)
    assert stored is not None and stored.state is PendingProductionState.AWAITING_CONFIRMATION
    assert stored.confirmed_at is None
    assert linked_jobs(session, pending.request_id) == []


def test_materialization_notifies_each_created_job_only_after_commit_and_not_on_replay(session: Session, now: datetime) -> None:
    pending = create_pending(session, now, quantity=2)
    observed: list[tuple[int, str | None, bool]] = []

    def callback(job_id: int, reason: str | None) -> None:
        observed.append((job_id, reason, session.get(ProductionJob, job_id) is not None))

    service = ProductionRequestMaterializationService(
        session, clock=lambda: now, post_commit_callback=callback
    )
    first = service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert observed == [(job.job_id, "job_created", True) for job in first.jobs]
    service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert observed == [(job.job_id, "job_created", True) for job in first.jobs]


def test_materializer_uses_process_local_registered_callback_for_api_owned_creation(session: Session, now: datetime) -> None:
    pending = create_pending(session, now, quantity=1)
    observed: list[tuple[int, str | None]] = []
    set_production_change_callback(lambda job_id, reason: observed.append((job_id, reason)))
    try:
        result = ProductionRequestMaterializationService(session, clock=lambda: now).confirm_and_create_jobs(
            pending_request_id=pending.request_id
        )
    finally:
        set_production_change_callback(None)
    assert observed == [(result.jobs[0].job_id, "job_created")]


def test_materialization_commit_failure_notifies_nothing_and_callback_failure_keeps_job(session: Session, now: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = create_pending(session, now, quantity=1)
    pending.session_id = "failed-callback"
    session.commit()
    observed: list[int] = []
    service = ProductionRequestMaterializationService(
        session, clock=lambda: now, post_commit_callback=lambda job_id, _reason: observed.append(job_id)
    )
    monkeypatch.setattr(service, "_create_job", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("forced failure")))
    with pytest.raises(RuntimeError, match="forced failure"):
        service.confirm_and_create_jobs(pending_request_id=pending.request_id)
    assert observed == []

    committed = create_pending(session, now, quantity=1)
    failing = ProductionRequestMaterializationService(
        session, clock=lambda: now, post_commit_callback=lambda _job_id, _reason: (_ for _ in ()).throw(RuntimeError("publisher unavailable"))
    )
    result = failing.confirm_and_create_jobs(pending_request_id=committed.request_id)
    assert result.created is True
    assert linked_jobs(session, committed.request_id) == result.jobs


def test_rejected_waiting_and_partial_links_are_never_materialized(session: Session, now: datetime) -> None:
    rejected = create_pending(session, now, quantity=1)
    PendingProductionRequestService(session, clock=lambda: now).reject_pending(request_id=rejected.request_id)
    with pytest.raises(PendingMaterializationStateError):
        materializer(session, now).confirm_and_create_jobs(pending_request_id=rejected.request_id)

    waiting = PendingProductionRequestService(session, clock=lambda: now).create_pending(session_id="waiting", product_code="HOUSE_A", quantity=1)
    with pytest.raises(PendingMaterializationStateError):
        materializer(session, now).confirm_and_create_jobs(pending_request_id=waiting.request_id)

    corrupt = create_pending(session, now, quantity=3, state=PendingProductionState.CONFIRMED)
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_B"))
    assert product is not None
    for index in (1, 2):
        session.add(ProductionJob(job_code=f"corrupt-{corrupt.request_id}-{index}", product_code=product.product_code, roof_option_code=RoofOptionCode.ROOF_02, source_pending_request_id=corrupt.request_id, source_item_index=index, status="REQUESTED"))
    session.commit()
    with pytest.raises(PendingMaterializationInvariantError):
        materializer(session, now).confirm_and_create_jobs(pending_request_id=corrupt.request_id)
    assert [job.source_item_index for job in linked_jobs(session, corrupt.request_id)] == [1, 2]

def test_a1_authoritative_recheck(session: Session, now: datetime) -> None:
    from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightResult, PreflightShortage
    from shared.services.production_request_materialization_service import ProductionInventoryShortageError

    pending = create_pending(session, now, quantity=1)

    class FakePreflightFailing:
        def validate(self, **kwargs):
            return ProductionInventoryPreflightResult(
                can_produce=False,
                product_code=pending.product_code,
                requested_quantity=pending.quantity,
                shortages=[
                    PreflightShortage(part_code="PART_A", part_name="외벽", required_quantity=2, available_quantity=0, shortage_quantity=2)
                ]
            )

    svc = ProductionRequestMaterializationService(session, clock=lambda: now, preflight_service=FakePreflightFailing())

    with pytest.raises(ProductionInventoryShortageError) as exc_info:
        svc.confirm_and_create_jobs(pending_request_id=pending.request_id)

    assert "preflight check failed during job creation" in str(exc_info.value)
    assert exc_info.value.shortages[0].part_code == "PART_A"

    # Confirm jobs were not created
    jobs = session.scalars(select(ProductionJob).where(ProductionJob.source_pending_request_id == pending.request_id)).all()
    assert len(jobs) == 0



def test_collecting_details_request_cannot_materialize(session: Session, now: datetime) -> None:
    pending = PendingProductionRequestService(session, clock=lambda: now).create_pending(
        session_id="incomplete", product_code=None, quantity=1
    )
    assert pending.state is PendingProductionState.COLLECTING_DETAILS
    with pytest.raises(PendingMaterializationStateError):
        ProductionRequestMaterializationService(session, clock=lambda: now).confirm_and_create_jobs(
            pending_request_id=pending.request_id
        )
