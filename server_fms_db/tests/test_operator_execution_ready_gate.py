from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from shared.models import Base
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    Part,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.operator_execution_ready_service import OperatorExecutionReadyService
from shared.services.manual_prestage_service import ManualPrestageService, ManualPrestageStateError
from shared.services.physical_ready_service import PhysicalReadyService, PhysicalReadyStateError
from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessReason, StepReadinessService
from tests.recipe_test_support import add_active_recipe, add_material_requirement, seed_complete_incoming_qa


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        product = Product(product_code="GENERIC_GATE", product_name="Generic gate fixture")
        db.add(product)
        db.flush()
        recipe = add_active_recipe(db, product.product_code, stages=[(1, "INSTALL_INNER_WALL", "generic")])
        add_material_requirement(db, recipe, part_code="GENERIC_GATE_PART", inventory_quantity=5, stage_order=1)
        stage = recipe.stages[0]
        stage.supply_mode = SupplyMode.TRANSPORTED
        stage.supply_group_code = "GENERIC_GATE_GROUP"
        stage.supply_destination_code = "ROBOT_CELL"
        db.commit()
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _job_and_step(session: Session):
    service = ProductionOrchestrationService(session)
    job = service.create_job(product_code="GENERIC_GATE", job_code="GENERIC-GATE-001")
    service.start_job(job.job_id)
    step = service.get_next_step(job.job_id)
    assert step is not None and step.supply_mode is SupplyMode.TRANSPORTED
    delivery = session.scalar(select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == job.job_id))
    assert delivery is not None
    seed_complete_incoming_qa(session, job_id=job.job_id)
    return job, step, delivery


def _complete_delivery(session: Session, delivery: JobMaterialDelivery) -> None:
    PhysicalReadyService(session).confirm_physical_ready(
        job_delivery_id=delivery.job_delivery_id, request_id="generic-gate-physical-ready"
    )
    deliveries = MaterialDeliveryService(session)
    deliveries.start_delivery(delivery.job_delivery_id)
    deliveries.complete_delivery(delivery.job_delivery_id)


def test_outer_wall_batch_operator_ready_uses_delivery_membership_atomically(
    session: Session, client: TestClient
) -> None:
    part = session.scalar(select(Part))
    assert part is not None
    job = ProductionJob(job_code="OUTER-BATCH", product_code="GENERIC_GATE", status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code="OUTER-BATCH",
        display_name="Outer batch", status=MaterialDeliveryStatus.COMPLETED, supply_mode=SupplyMode.TRANSPORTED,
        supply_group_code="OUTER_WALLS", supply_destination_code="ROBOT_CELL",
    )
    session.add(delivery)
    session.flush()
    steps = []
    for order in (40, 10, 30, 20):
        step = JobStep(
            job_id=job.job_id, step_order=order, operation_code="INSTALL_INNER_WALL",
            display_name="Generic group member", part_code=part.part_code, quantity=1,
            supply_mode=SupplyMode.TRANSPORTED, supply_group_code="OUTER_WALLS",
            supply_destination_code="ROBOT_CELL", status=StepStatus.PENDING,
        )
        session.add(step)
        session.flush()
        session.add(JobMaterialDeliveryItem(
            job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id,
            part_code=part.part_code, quantity=1,
        ))
        steps.append(step)
    session.commit()

    returned_delivery, authorized = OperatorExecutionReadyService(session).confirm_outer_walls_batch(
        job_id=job.job_id
    )
    assert returned_delivery.job_delivery_id == delivery.job_delivery_id
    assert [step.step_order for step in authorized] == [10, 20, 30, 40]
    assert all(step.operator_execution_ready_at is not None for step in authorized)
    assert session.scalar(select(JobStep).where(JobStep.job_id == job.job_id, JobStep.status == StepStatus.RUNNING)) is None

    completed = authorized[0]
    completed.status = StepStatus.COMPLETED
    session.commit()
    _, repeated = OperatorExecutionReadyService(session).confirm_outer_walls_batch(job_id=job.job_id)
    assert completed.status is StepStatus.COMPLETED
    assert [step.job_step_id for step in repeated] == [step.job_step_id for step in authorized]
    endpoint = client.post(f"/production/jobs/{job.job_id}/outer-walls/operator-ready")
    assert endpoint.status_code == 200
    assert endpoint.json()["job_delivery_id"] == delivery.job_delivery_id
    assert endpoint.json()["job_step_ids"] == [step.job_step_id for step in authorized]


def test_transported_delivery_completed_requires_durable_operator_release(session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    _complete_delivery(session, delivery)

    readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id
    )
    assert readiness.ready is False
    assert readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED


def test_execution_ready_endpoint_rejects_early_click_and_is_idempotent(client: TestClient, session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    url = f"/production/jobs/{job.job_id}/steps/{step.job_step_id}/execution-ready"

    early = client.post(url)
    assert early.status_code == 409
    assert session.get(JobStep, step.job_step_id).operator_execution_ready_at is None

    _complete_delivery(session, delivery)
    accepted = client.post(url)
    assert accepted.status_code == 200
    first = accepted.json()["operator_execution_ready_at"]
    duplicate = client.post(url)
    assert duplicate.status_code == 200
    assert duplicate.json()["operator_execution_ready_at"] == first

    readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id
    )
    assert readiness.ready is True


def test_manual_base_style_step_is_not_given_the_transported_gate(session: Session) -> None:
    job, step, _delivery = _job_and_step(session)
    step.supply_mode = SupplyMode.MANUAL
    session.commit()

    snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=job.job_id)
    assert snapshot.next_step is not None
    assert snapshot.next_step.operator_execution_ready_at is None
    assert snapshot.next_step.readiness_reason != StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED.value


def test_transported_roof_operation_uses_the_same_generic_gate(session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    step.operation_code = "INSTALL_ROOF"
    session.commit()
    _complete_delivery(session, delivery)

    readiness = StepReadinessService(MaterialDeliveryService(session)).evaluate(
        job_id=job.job_id, job_step_id=step.job_step_id
    )
    assert readiness.reason is StepReadinessReason.OPERATOR_EXECUTION_READY_REQUIRED


def test_snapshot_exposes_canonical_operator_release_state(client: TestClient, session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    _complete_delivery(session, delivery)
    url = f"/production/jobs/{job.job_id}/steps/{step.job_step_id}/execution-ready"
    assert client.post(url).status_code == 200

    response = client.get(f"/production/jobs/{job.job_id}/execution-snapshot")
    assert response.status_code == 200
    next_step = response.json()["next_step"]
    assert next_step["supply_mode"] == "TRANSPORTED"
    assert next_step["operator_execution_ready_at"] is not None
    assert next_step["readiness_reason"] is None


def test_non_pending_step_cannot_be_newly_released(client: TestClient, session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    _complete_delivery(session, delivery)
    step.status = StepStatus.RUNNING
    session.commit()

    response = client.post(f"/production/jobs/{job.job_id}/steps/{step.job_step_id}/execution-ready")
    assert response.status_code == 409

def test_operator_ready_post_commit_callback_is_once_only(session: Session) -> None:
    job, step, delivery = _job_and_step(session)
    _complete_delivery(session, delivery)
    observed: list[tuple[int, str | None]] = []
    service = OperatorExecutionReadyService(session, post_commit_callback=lambda job_id, reason: observed.append((job_id, reason)))
    service.confirm(job_id=job.job_id, job_step_id=step.job_step_id)
    service.confirm(job_id=job.job_id, job_step_id=step.job_step_id)
    assert observed == [(job.job_id, "operator_execution_ready")]


def test_manual_prestage_post_commit_callback_is_once_only(session: Session) -> None:
    job, _step, delivery = _job_and_step(session)
    delivery.supply_mode = SupplyMode.MANUAL
    delivery.supply_group_code = "MANUAL_GATE_GROUP"
    session.commit()
    observed: list[tuple[int, str | None]] = []
    service = ManualPrestageService(session, post_commit_callback=lambda job_id, reason: observed.append((job_id, reason)))
    service.confirm_manual_prestage_ready(job_id=job.job_id, job_delivery_id=delivery.job_delivery_id, request_id="manual-post-commit")
    service.confirm_manual_prestage_ready(job_id=job.job_id, job_delivery_id=delivery.job_delivery_id, request_id="manual-post-commit-retry")
    assert observed == [(job.job_id, "manual_prestage_ready")]


def test_terminal_job_rejects_operator_manual_and_physical_ready_mutations(session: Session, client: TestClient) -> None:
    job, step, delivery = _job_and_step(session)
    job.status = JobStatus.CANCELED
    delivery.supply_mode = SupplyMode.MANUAL
    delivery.supply_group_code = "MANUAL_GATE_GROUP"
    session.commit()

    assert client.post(
        f"/production/jobs/{job.job_id}/steps/{step.job_step_id}/execution-ready"
    ).status_code == 409
    with pytest.raises(ManualPrestageStateError, match="terminal production job"):
        ManualPrestageService(session).confirm_manual_prestage_ready(
            job_id=job.job_id,
            job_delivery_id=delivery.job_delivery_id,
            request_id="terminal-manual",
        )

    delivery.supply_mode = SupplyMode.TRANSPORTED
    delivery.supply_group_code = "GENERIC_GATE_GROUP"
    session.commit()
    with pytest.raises(PhysicalReadyStateError, match="terminal production job"):
        PhysicalReadyService(session).confirm_physical_ready(
            job_delivery_id=delivery.job_delivery_id, request_id="terminal-physical"
        )
