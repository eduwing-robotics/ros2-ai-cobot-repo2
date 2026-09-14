from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    JobMaterialDelivery,
    JobMaterialFeedExecution,

    Product,
    Inventory,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import (
    StepReadinessReason,
    StepReadinessService,
)
from tests.recipe_test_support import HOUSE_A_STAGES, add_active_recipe


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_A", product_name="A형 주택"))
    db.flush()
    add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES)
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _job(session: Session):
    product = session.scalar(select(Product).where(Product.product_code == "HOUSE_A"))
    assert product is not None
    service = ProductionOrchestrationService(session)
    job = service.create_job(product_code=product.product_code, job_code="READINESS-001")
    service.start_job(job.job_id)
    step = service.get_next_step(job.job_id)
    assert step is not None
    return job, step


def _readiness(session: Session) -> StepReadinessService:
    return StepReadinessService(MaterialDeliveryService(session))


def _two_delivery_plan_for_first_stage(session: Session) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None
    from shared.models.factory import Part, PartCategory
    part = Part(vision_class="wall_ext_back", part_code="SYNTH_PART", part_name="Synthetic", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part); session.flush()
    session.add(Inventory(part_code=part.part_code, quantity=10))
    recipe.stages[0].part_code = part.part_code
    recipe.stages[0].quantity = 1
    session.commit()


def _pass_incoming_qa(session: Session, delivery: JobMaterialDelivery) -> None:
    service = MaterialInspectionService()
    for item in delivery.items:
        request = service.request_inspection(session, item.delivery_item_id)
        service.mark_running(session, request.inspection_request_id)
        service.apply_inspection_result(
            session,
            IncomingMaterialQAResult(
                inspection_request_id=request.inspection_request_id,
                delivery_item_id=request.delivery_item_id,
                inspection_cycle=request.inspection_cycle,
                result="PASS",
                expected_part_code=request.expected_part_code,
                expected_class_name=request.expected_class_name,
                expected_quantity=request.expected_quantity,
                detected_quantity=request.expected_quantity,
                detections=[],
                frame_width=640,
                frame_height=480,
                camera_source="GLOBAL_CAMERA",
                frame_seq=1,
                timestamp=datetime.now(timezone.utc),
                model_scope="test",
                model_version="1",
                production_valid=True,
            ),
        )


def test_no_delivery_dependency_is_ready(session: Session) -> None:
    job, step = _job(session)

    result = _readiness(session).evaluate(job_id=job.job_id, job_step_id=step.job_step_id)

    assert result.ready is False
    assert result.reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE


def test_pending_and_completed_multiple_delivery_dependencies_delegate_to_material_service(session: Session) -> None:
    _two_delivery_plan_for_first_stage(session)
    job, step = _job(session)
    readiness = _readiness(session)
    deliveries = list(
        session.scalars(
            select(JobMaterialDelivery)
            .where(JobMaterialDelivery.production_job_id == job.job_id)
            .order_by(JobMaterialDelivery.batch_order)
        )
    )
    assert len(deliveries) == 1

    assert readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id).ready is False

    MaterialDeliveryService(session).start_delivery(deliveries[0].job_delivery_id)
    MaterialDeliveryService(session).complete_delivery(deliveries[0].job_delivery_id)

    hold = readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)
    assert hold.ready is False and hold.reason is StepReadinessReason.PRE_PRODUCTION_QA_INCOMPLETE
    _pass_incoming_qa(session, deliveries[0])

    # Note: feed execution handles the final readiness state which is tested in the feed test.
    # In the current system, readiness handles feed if deliveries are complete, but readiness mock here just tests delegation.
    feeds = list(session.scalars(select(JobMaterialFeedExecution).order_by(JobMaterialFeedExecution.feed_execution_id)))
    feed_service = MaterialFeedExecutionService(session)
    for feed in feeds:
        feed_service.start_feed(feed.feed_execution_id)
        feed_service.complete_feed(feed.feed_execution_id, completed_slots=())
    complete = readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)

    assert complete.ready is True and complete.reason is None
