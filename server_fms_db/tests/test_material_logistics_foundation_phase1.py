from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    Base,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialFeedStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.material_delivery_service import (
    MaterialDeliveryConfigurationError,
    MaterialDeliveryService,
)
from shared.services.physical_ready_service import (
    PhysicalReadyPolicyError,
    PhysicalReadyService,
    PhysicalReadyStateError,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import seed_inventory_for_recipe


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _recipe_with_steps(session: Session, policies: list[tuple[SupplyMode | None, str | None, str | None]]) -> Product:
    product = Product(product_code="LOGISTICS_TEST", product_name="Logistics test")
    session.add(product)
    parts = []
    for index in range(len(policies)):
        part = Part(
            part_code=f"LOGISTICS_PART_{index + 1}",
            part_name=f"Logistics part {index + 1}",
            category=PartCategory.STRUCTURE,
            unit="EA",
        )
        parts.append(part)
        session.add(part)
    session.flush()
    recipe = AssemblyRecipe(product_code=product.product_code, version=1, is_active=True)
    session.add(recipe)
    session.flush()
    for index, (mode, group, destination) in enumerate(policies, start=1):
        session.add(
            AssemblyRecipeStage(
                recipe_id=recipe.recipe_id,
                stage_order=index,
                operation_code="INSTALL_TEST",
                display_name=f"Install {index}",
                part_code=parts[index - 1].part_code,
                quantity=1,
                supply_mode=mode,
                supply_group_code=group,
                supply_destination_code=destination,
                is_terminal=index == len(policies),
            )
        )
    seed_inventory_for_recipe(session, recipe)
    session.commit()
    return product


def _create_job(session: Session, product: Product, code: str = "LOGISTICS-JOB") -> ProductionJob:
    return ProductionOrchestrationService(session).create_job(
        product_code=product.product_code,
        job_code=code,
    )


def _direct_job_with_steps(
    session: Session,
    policies: list[tuple[SupplyMode | None, str | None, str | None]],
    code: str,
) -> ProductionJob:
    product = Product(product_code=f"{code}-PRODUCT", product_name=code)
    session.add(product)
    session.flush()
    job = ProductionJob(job_code=code, product_code=product.product_code, status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    for index, (mode, group, destination) in enumerate(policies, start=1):
        part = Part(
            part_code=f"{code}-PART-{index}",
            part_name=f"{code} part {index}",
            category=PartCategory.STRUCTURE,
            unit="EA",
        )
        session.add(part)
        session.flush()
        session.add(
            JobStep(
                job_id=job.job_id,
                step_order=index,
                operation_code="INSTALL_TEST",
                display_name=f"Install {index}",
                part_code=part.part_code,
                quantity=1,
                supply_mode=mode,
                supply_group_code=group,
                supply_destination_code=destination,
                status=StepStatus.PENDING,
            )
        )
    session.flush()
    session.refresh(job)
    return job


def test_recipe_policy_is_immutable_jobstep_snapshot(session: Session) -> None:
    product = _recipe_with_steps(session, [(SupplyMode.TRANSPORTED, "TEST_GROUP_A", "TEST_DEST_A")])
    job = _create_job(session, product)
    step = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id))
    assert step is not None
    assert (step.supply_mode, step.supply_group_code, step.supply_destination_code) == (
        SupplyMode.TRANSPORTED,
        "TEST_GROUP_A",
        "TEST_DEST_A",
    )

    stage = session.scalar(select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == job.assembly_recipe_id))
    assert stage is not None
    stage.supply_mode = SupplyMode.MANUAL
    stage.supply_group_code = "TEST_GROUP_CHANGED"
    stage.supply_destination_code = "TEST_DEST_CHANGED"
    session.commit()
    session.refresh(step)
    assert (step.supply_mode, step.supply_group_code, step.supply_destination_code) == (
        SupplyMode.TRANSPORTED,
        "TEST_GROUP_A",
        "TEST_DEST_A",
    )


def test_grouped_delivery_partitions_items_and_is_idempotent(session: Session) -> None:
    product = _recipe_with_steps(
        session,
        [
            (SupplyMode.TRANSPORTED, "TEST_GROUP_STRUCTURE", "TEST_DEST_STRUCTURE"),
            (SupplyMode.MANUAL, "TEST_GROUP_MANUAL", None),
        ],
    )
    job = _create_job(session, product)
    service = MaterialDeliveryService(session)
    deliveries = service.get_deliveries_for_job(job.job_id)
    assert [delivery.supply_group_code for delivery in deliveries] == [
        "TEST_GROUP_STRUCTURE",
        "TEST_GROUP_MANUAL",
    ]
    assert len(deliveries) == 2
    assert all(len(delivery.items) == 1 for delivery in deliveries)
    assert {delivery.items[0].job_step.supply_group_code for delivery in deliveries} == {
        "TEST_GROUP_MANUAL",
        "TEST_GROUP_STRUCTURE",
    }
    assert session.scalar(select(func.count()).select_from(JobMaterialFeedExecution)) == 0

    repeated = service.instantiate_for_job(job=job)
    assert {delivery.job_delivery_id for delivery in repeated} == {delivery.job_delivery_id for delivery in deliveries}
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery)) == 2
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem)) == 2


def test_legacy_null_policy_keeps_one_delivery_and_is_idempotent(session: Session) -> None:
    product = _recipe_with_steps(session, [(None, None, None), (None, None, None)])
    job = _create_job(session, product)
    service = MaterialDeliveryService(session)
    deliveries = service.get_deliveries_for_job(job.job_id)
    assert len(deliveries) == 1
    assert deliveries[0].supply_mode is None
    assert deliveries[0].supply_group_code is None
    assert len(deliveries[0].items) == 2
    repeated = service.instantiate_for_job(job=job)
    assert [item.job_delivery_id for item in repeated] == [deliveries[0].job_delivery_id]
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery)) == 1
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem)) == 2


@pytest.mark.parametrize(
    "policies",
    [
        [(SupplyMode.TRANSPORTED, "TEST_CONFLICT", None), (SupplyMode.MANUAL, "TEST_CONFLICT", None)],
        [(SupplyMode.TRANSPORTED, "TEST_CONFLICT", "DEST_A"), (SupplyMode.TRANSPORTED, "TEST_CONFLICT", "DEST_B")],
        [(SupplyMode.TRANSPORTED, "TEST_NEW", None), (None, None, None)],
    ],
)
def test_inconsistent_policy_fails_closed(session: Session, policies) -> None:
    job = _direct_job_with_steps(session, policies, code=f"CONFLICT-{len(policies)}-{policies[0][1]}")
    with pytest.raises(MaterialDeliveryConfigurationError):
        MaterialDeliveryService(session).instantiate_for_job(job=job)
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery)) == 0


def test_physical_ready_is_one_way_idempotent_and_dormant(session: Session) -> None:
    product = _recipe_with_steps(session, [(SupplyMode.TRANSPORTED, "TEST_READY_GROUP", None)])
    job = _create_job(session, product)
    delivery = MaterialDeliveryService(session).get_deliveries_for_job(job.job_id)[0]
    feed = session.scalar(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == delivery.job_delivery_id))
    assert delivery.physical_ready_at is None
    assert feed is None

    service = PhysicalReadyService(session)
    first = service.confirm_physical_ready(job_delivery_id=delivery.job_delivery_id, request_id="ready-request-1")
    first_time = first.physical_ready_at
    assert first_time is not None
    assert first.physical_ready_request_id == "ready-request-1"
    assert first.status is MaterialDeliveryStatus.PENDING
    same = service.confirm_physical_ready(job_delivery_id=delivery.job_delivery_id, request_id="ready-request-1")
    other = service.confirm_physical_ready(job_delivery_id=delivery.job_delivery_id, request_id="ready-request-2")
    assert same.physical_ready_at == first_time
    assert other.physical_ready_at == first_time
    assert other.physical_ready_request_id == "ready-request-1"
    assert session.scalar(select(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id == delivery.job_delivery_id)) is None


@pytest.mark.parametrize("status", [MaterialDeliveryStatus.IN_PROGRESS, MaterialDeliveryStatus.COMPLETED])
def test_physical_ready_rejects_legacy_and_started_delivery(session: Session, status: MaterialDeliveryStatus) -> None:
    legacy_product = _recipe_with_steps(session, [(None, None, None)])
    legacy_job = _create_job(session, legacy_product, "LEGACY-READY")
    legacy_delivery = MaterialDeliveryService(session).get_deliveries_for_job(legacy_job.job_id)[0]
    with pytest.raises(PhysicalReadyPolicyError):
        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=legacy_delivery.job_delivery_id,
            request_id="legacy-request",
        )

    job = _direct_job_with_steps(session, [(SupplyMode.TRANSPORTED, "TEST_STARTED", None)], "STARTED-READY")
    delivery = MaterialDeliveryService(session).instantiate_for_job(job=job)[0]
    delivery.status = status
    session.commit()
    with pytest.raises(PhysicalReadyStateError):
        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=delivery.job_delivery_id,
            request_id="started-request",
        )
    assert delivery.physical_ready_at is None
