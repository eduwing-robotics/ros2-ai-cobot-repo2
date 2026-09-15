from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models.factory import (
    Base,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    Inventory,
    MaterialDeliveryStatus,
    Part,
    PendingProductionRequest,
    Product,
    ProductionJob,
    RoofOptionCode,
)
from shared.services.material_delivery_service import (
    InvalidMaterialDeliveryStateTransitionError,
    MaterialDeliveryService,
)
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.production_request_materialization_service import (
    ProductionRequestMaterializationService,
)
from tests.recipe_test_support import add_active_recipe


@pytest.fixture(name="session")
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(Product(product_code="HOUSE_A", product_name="House A"))
        session.add(Product(product_code="HOUSE_B", product_name="House B"))
        session.flush()
        recipe = add_active_recipe(session, "HOUSE_A", materialized=False)

        # Add a part and assign it to a stage
        part = Part(vision_class="wall_ext_back", part_code="TEST_MATERIAL", part_name="Test part", category="STRUCTURE", unit="EA")
        session.add(part)
        session.flush()
        session.add(Inventory(part_code=part.part_code, quantity=10))
        stage = recipe.stages[0]
        stage.part_code = part.part_code
        stage.quantity = 1
        session.flush()
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_instantiate_for_job_derives_deliveries_from_job_steps(session: Session) -> None:
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    job = ProductionOrchestrationService(session).create_job(
        product_code=product.product_code, job_code="DELIVERY-001", roof_option_code=RoofOptionCode.ROOF_01
    )

    deliveries = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)
    assert len(deliveries) == 1
    delivery = deliveries[0]

    assert delivery.status == MaterialDeliveryStatus.PENDING
    assert len(delivery.items) == 1
    item = delivery.items[0]

    # Every item must link back to a job step
    assert item.job_step_id is not None
    step = session.get(JobStep, item.job_step_id)
    assert step is not None
    assert step.job_id == job.job_id
    assert item.part_code == step.part_code
    assert item.quantity == step.quantity

    # Test readiness
    service = MaterialDeliveryService(session)
    assert service.is_material_ready_for_step(step.job_step_id) is False

    service.start_delivery(delivery.job_delivery_id)
    service.complete_delivery(delivery.job_delivery_id)
    assert service.is_material_ready_for_step(step.job_step_id) is True


def test_delivery_snapshot_failure_rolls_back_pending_job_and_runtime_rows(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    pending = PendingProductionRequestService(session, clock=lambda: now).create_pending(
        session_id="delivery-rollback", product_code="HOUSE_A", quantity=2, roof_option_code=RoofOptionCode.ROOF_02
    )
    original = MaterialDeliveryService.instantiate_for_job
    calls = {"count": 0}

    from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightResult
    monkeypatch.setattr("shared.services.production_inventory_preflight_service.ProductionInventoryPreflightService.validate", lambda *args, **kwargs: ProductionInventoryPreflightResult(can_produce=True, product_code="HOUSE_A", requested_quantity=2, shortages=[]))

    def fail_second(self, **kwargs):
        calls["count"] += 1
        result = original(self, **kwargs)
        if calls["count"] == 2:
            raise RuntimeError("forced delivery snapshot failure")
        return result

    monkeypatch.setattr(MaterialDeliveryService, "instantiate_for_job", fail_second)
    with pytest.raises(RuntimeError, match="forced delivery snapshot failure"):
        ProductionRequestMaterializationService(session, clock=lambda: now).confirm_and_create_jobs(
            pending_request_id=pending.request_id
        )
    stored = session.get(PendingProductionRequest, pending.request_id)
    assert stored is not None and stored.confirmed_at is None
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0
    assert session.scalar(select(func.count()).select_from(JobStep)) == 0
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery)) == 0
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem)) == 0


def test_delivery_completion_notifies_only_after_successful_commit(session: Session) -> None:
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    job = ProductionOrchestrationService(session).create_job(product_code=product.product_code, job_code="DELIVERY-CALLBACK", roof_option_code=RoofOptionCode.ROOF_01)
    delivery = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)[0]
    observed: list[tuple[int, str | None, MaterialDeliveryStatus]] = []
    service = MaterialDeliveryService(session, post_commit_callback=lambda job_id, reason: observed.append((job_id, reason, delivery.status)))
    with pytest.raises(InvalidMaterialDeliveryStateTransitionError):
        service.complete_delivery(delivery.job_delivery_id)
    assert observed == []
    service.start_delivery(delivery.job_delivery_id)
    service.complete_delivery(delivery.job_delivery_id)
    assert observed == [(job.job_id, "material_delivery_completed", MaterialDeliveryStatus.COMPLETED)]
    with pytest.raises(InvalidMaterialDeliveryStateTransitionError):
        service.complete_delivery(delivery.job_delivery_id)
    assert len(observed) == 1

def test_delivery_callback_failure_does_not_undo_committed_completion(session: Session) -> None:
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    job = ProductionOrchestrationService(session).create_job(product_code=product.product_code, job_code="DELIVERY-CALLBACK-FAIL", roof_option_code=RoofOptionCode.ROOF_01)
    delivery = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)[0]
    service = MaterialDeliveryService(session, post_commit_callback=lambda *_args: (_ for _ in ()).throw(RuntimeError("publisher unavailable")))
    service.start_delivery(delivery.job_delivery_id)
    assert service.complete_delivery(delivery.job_delivery_id).status is MaterialDeliveryStatus.COMPLETED
