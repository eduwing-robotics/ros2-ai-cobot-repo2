from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from fms_server.material_prefetch_service import MaterialPrefetchService
from shared.models.factory import (
    Base,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialDeliveryStatus,
    StepStatus,
    SupplyMode,
)

from tests.test_material_logistics_phase3a_transported_readiness import _new_delivery


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with session_factory() as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_selects_only_nearest_future_transported_delivery_and_deduplicates_shared_delivery(
    session: Session,
) -> None:
    """Selection follows binding/order only, never product or operation names."""
    job, _, _ = _new_delivery(
        session,
        mode=SupplyMode.MANUAL,
        group="GENERIC_MANUAL_CURRENT",
        suffix="GENERIC_CURRENT",
        step_order=1,
        operation_code="STEP_ALPHA",
    )
    _, first_step, first_delivery = _new_delivery(
        session,
        job=job,
        mode=SupplyMode.TRANSPORTED,
        group="GENERIC_TRANSPORT_A",
        suffix="GENERIC_A",
        step_order=3,
        operation_code="STEP_BETA",
    )
    _, _, second_delivery = _new_delivery(
        session,
        job=job,
        mode=SupplyMode.TRANSPORTED,
        group="GENERIC_TRANSPORT_B",
        suffix="GENERIC_B",
        step_order=5,
        operation_code="STEP_GAMMA",
    )

    # The first delivery supplies two future steps. It remains one candidate.
    shared_step = JobStep(
        job_id=job.job_id,
        step_order=4,
        operation_code="STEP_DELTA",
        display_name="Generic shared consumer",
        part_code=first_step.part_code,
        quantity=1,
        slot_code="GENERIC_SHARED_SLOT",
        pick_zone="GENERIC_ZONE",
        vision_class=first_step.vision_class,
        supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="GENERIC_TRANSPORT_A",
        status=StepStatus.PENDING,
    )
    session.add(shared_step)
    session.flush()
    session.add(
        JobMaterialDeliveryItem(
            job_delivery_id=first_delivery.job_delivery_id,
            job_step_id=shared_step.job_step_id,
            part_code=first_step.part_code,
            quantity=1,
        )
    )
    session.commit()

    selector = MaterialPrefetchService(session)
    candidate = selector.nearest_future_transported_delivery(
        job_id=job.job_id,
        execution_frontier_step_order=1,
    )

    assert candidate is not None
    assert candidate.job_delivery_id == first_delivery.job_delivery_id
    assert candidate.earliest_consuming_step_order == 3

    first_delivery.status = MaterialDeliveryStatus.COMPLETED
    session.commit()
    next_candidate = selector.nearest_future_transported_delivery(
        job_id=job.job_id,
        execution_frontier_step_order=1,
    )
    assert next_candidate is not None
    assert next_candidate.job_delivery_id == second_delivery.job_delivery_id
    assert next_candidate.earliest_consuming_step_order == 5
