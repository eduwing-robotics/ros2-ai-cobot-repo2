from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base
from shared.models.factory import (
    Inventory,
    InventoryMovement,
    ExecutionAttemptStatus,
    ExecutorType,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    Part,
    PartCategory,
    Product,
    RoofOptionCode,
    StepStatus,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.inventory_reservation_service import (
    InsufficientAvailableInventoryError,
    InventoryReservationService,
)
from fms_server.worker import FmsWorker
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import (
    InvalidProductionStateTransitionError,
    ProductionOrchestrationService,
)
from tests.recipe_test_support import add_active_recipe, add_gated_roof_stages


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_recipe(session: Session, *, initial_quantity: int = 1, initial_stock: int = 20, roof_stock: int = 20) -> Product:
    product = Product(product_code="GENERIC_TEST", product_name="Generic test product")
    session.add(product)
    session.flush()
    recipe = add_active_recipe(session, product.product_code, stages=[(1, "INSTALL_BASE", "Base")])
    add_gated_roof_stages(session, recipe)
    initial_stage = recipe.stages[0]
    assert initial_stage.part_code is not None
    initial_stage.quantity = initial_quantity
    session.get(Inventory, initial_stage.part_code).quantity = initial_stock
    for code in ("ROOF_01", "ROOF_02"):
        session.get(Inventory, code).quantity = roof_stock
    session.commit()
    return product


def _create(session: Session, product: Product, code: str) -> object:
    return ProductionOrchestrationService(session).create_job(
        product_code=product.product_code,
        job_code=code,
        roof_option_code=RoofOptionCode.ROOF_01,
    )


def _initial_part(session: Session, job_id: int) -> str:
    step = session.scalar(select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.step_order))
    assert step is not None and step.part_code is not None
    return step.part_code


def test_creation_reserves_full_recipe_including_unmaterialized_roof(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "RESERVE-FULL")
    part_code = _initial_part(session, job.job_id)

    initial = session.get(Inventory, part_code)
    roof = session.get(Inventory, "ROOF_01")
    assert initial is not None and (initial.quantity, initial.reserved_quantity, initial.available_quantity) == (20, 1, 19)
    assert roof is not None and (roof.quantity, roof.reserved_quantity, roof.available_quantity) == (20, 1, 19)
    assert session.scalars(select(JobStep).where(JobStep.job_id == job.job_id, JobStep.operation_code == "INSTALL_ROOF")).first() is None
    deferred_item = session.scalar(
        select(JobMaterialDeliveryItem)
        .join(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job.job_id, JobMaterialDeliveryItem.part_code == "ROOF_01")
    )
    assert deferred_item is not None and deferred_item.job_step_id is None


def test_second_job_cannot_oversubscribe_reserved_inventory(session: Session) -> None:
    product = _seed_recipe(session, initial_quantity=7, initial_stock=10, roof_stock=10)
    first = _create(session, product, "RESERVE-A")
    part_code = _initial_part(session, first.job_id)
    assert session.get(Inventory, part_code).reserved_quantity == 7

    with pytest.raises(InsufficientAvailableInventoryError, match="available"):
        _create(session, product, "RESERVE-B")

    stored = session.get(Inventory, part_code)
    assert stored is not None and (stored.quantity, stored.reserved_quantity) == (10, 7)
    assert session.scalar(select(JobStep).where(JobStep.job_id == first.job_id)) is not None


def test_failed_creation_rolls_back_partial_reservation(session: Session) -> None:
    product = _seed_recipe(session, initial_stock=10, roof_stock=0)
    part_code = session.scalar(select(Part.part_code).where(Part.part_code.like("TEST_MATERIAL%")))
    assert part_code is not None

    with pytest.raises(InsufficientAvailableInventoryError):
        _create(session, product, "RESERVE-ROLLBACK")

    initial = session.get(Inventory, part_code)
    assert initial is not None and initial.reserved_quantity == 0
    assert session.scalar(select(JobStep).where(JobStep.job_id == 1)) is None


def test_consumption_after_accepted_execution_moves_physical_and_reservation_once(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "CONSUME-ONCE")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    part_code = step.part_code

    running = orchestration.start_step_after_execution_accepted(step.job_step_id)
    assert running.status is StepStatus.RUNNING and running.inventory_consumed_at is not None
    inventory = session.get(Inventory, part_code)
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity, inventory.available_quantity) == (19, 0, 19)
    movements = list(session.scalars(select(InventoryMovement).where(InventoryMovement.job_step_id == step.job_step_id)))
    assert len(movements) == 1 and movements[0].quantity == 1

    # A duplicate accepted callback cannot consume a second time: the Step is
    # already RUNNING and the durable marker/movement remain singular.
    with pytest.raises(InvalidProductionStateTransitionError):
        orchestration.start_step_after_execution_accepted(step.job_step_id)
    assert session.get(Inventory, part_code).quantity == 19
    assert session.scalars(select(InventoryMovement).where(InventoryMovement.job_step_id == step.job_step_id)).all() == movements


def test_cancel_releases_only_unconsumed_reservations(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "CANCEL-PARTIAL")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    part_code = step.part_code
    orchestration.start_step_after_execution_accepted(step.job_step_id)
    orchestration.cancel_job(job.job_id, reason="operator stop")

    initial = session.get(Inventory, part_code)
    roof = session.get(Inventory, "ROOF_01")
    assert initial is not None and (initial.quantity, initial.reserved_quantity) == (19, 0)
    assert roof is not None and (roof.quantity, roof.reserved_quantity) == (20, 0)
    stored_job = session.get(type(job), job.job_id)
    assert stored_job is not None and stored_job.status is JobStatus.CANCELED
    assert stored_job.inventory_reservation_released_at is not None


def test_cancel_before_execution_releases_all_without_physical_stock_change(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "CANCEL-BEFORE")
    orchestration = ProductionOrchestrationService(session)
    orchestration.cancel_job(job.job_id, reason="operator stop")
    initial = session.get(Inventory, _initial_part(session, job.job_id))
    roof = session.get(Inventory, "ROOF_01")
    assert initial is not None and (initial.quantity, initial.reserved_quantity) == (20, 0)
    assert roof is not None and (roof.quantity, roof.reserved_quantity) == (20, 0)


def test_pre_dispatch_states_do_not_consume(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "NO-PRE-DISPATCH-CONSUME")
    step = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id))
    assert step is not None and step.part_code is not None
    inventory = session.get(Inventory, step.part_code)
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity) == (20, 1)
    assert step.inventory_consumed_at is None


def test_accepted_failed_step_keeps_consumed_stock_and_releases_future_reservations(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "FAIL-KEEP-ISSUED")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    part_code = step.part_code
    orchestration.start_step_after_execution_accepted(step.job_step_id)
    orchestration.fail_step(step.job_step_id, reason="Cell execution failed after accepted start")

    issued = session.get(Inventory, part_code)
    reserved_roof = session.get(Inventory, "ROOF_01")
    assert issued is not None and (issued.quantity, issued.reserved_quantity) == (19, 0)
    assert reserved_roof is not None and (reserved_roof.quantity, reserved_roof.reserved_quantity) == (20, 0)
    assert session.get(type(job), job.job_id).status is JobStatus.FAILED


def test_pre_roof_materialized_roof_consumes_its_initial_reservation(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "ROOF-RESERVATION")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    initial = orchestration.get_next_step(job.job_id)
    assert initial is not None
    orchestration.start_step_after_execution_accepted(initial.job_step_id)
    orchestration.complete_step(initial.job_step_id)
    assert session.get(type(job), job.job_id).status.name == "PRE_ROOF_READY"

    completion = ProductionCompletionService(session)
    completion.start_pre_roof_inspection(production_job_id=job.job_id)
    roof = completion.pass_pre_roof_inspection(production_job_id=job.job_id)
    assert roof.operation_code == "INSTALL_ROOF"
    orchestration.start_step_after_execution_accepted(roof.job_step_id)

    inventory = session.get(Inventory, "ROOF_01")
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity) == (19, 0)


def _persist_accepted_cell_attempt(session: Session, *, job_id: int, step_id: int, req_id: str):
    """Model the durable state immediately after the ROS Goal acceptance callback."""
    attempts = ExecutionAttemptService(session)
    attempts.create_attempt(
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="GENERIC_INSTALL",
        request_payload={"job_step_id": step_id},
        job_id=job_id,
        job_step_id=step_id,
        req_id=req_id,
    )
    attempts.mark_dispatching(req_id)
    return attempts.mark_accepted(req_id)


def test_accepted_before_consumption_recovery_issues_once_and_preserves_stock(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "ACCEPTED-CRASH-RECOVERY")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    part_code = step.part_code
    attempt = _persist_accepted_cell_attempt(
        session, job_id=job.job_id, step_id=step.job_step_id, req_id="accepted-crash-recovery"
    )

    # Simulate process death at the durable ACCEPTED boundary: no Step start
    # and no inventory marker were committed by the original process.
    session.expire_all()
    step = session.get(JobStep, step.job_step_id)
    assert step is not None and step.status is StepStatus.PENDING and step.inventory_consumed_at is None
    assert attempt.goal_accepted_at is not None
    before = session.get(Inventory, part_code)
    assert before is not None and (before.quantity, before.reserved_quantity) == (20, 1)

    assert FmsWorker._recover_accepted_inventory_issue(
        session=session,
        job=session.get(type(job), job.job_id),
        step=step,
        attempt=attempt,
    ) is True
    session.expire_all()
    recovered = session.get(JobStep, step.job_step_id)
    inventory = session.get(Inventory, part_code)
    assert recovered is not None and recovered.inventory_consumed_at is not None
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity) == (19, 0)
    assert len(session.scalars(select(InventoryMovement).where(InventoryMovement.job_step_id == step.job_step_id)).all()) == 1

    # Duplicate restart reconciliation is a durable no-op.
    assert FmsWorker._recover_accepted_inventory_issue(
        session=session,
        job=session.get(type(job), job.job_id),
        step=recovered,
        attempt=session.get(type(attempt), attempt.attempt_id),
    ) is False
    assert len(session.scalars(select(InventoryMovement).where(InventoryMovement.job_step_id == step.job_step_id)).all()) == 1


def test_accepted_but_unconsumed_is_not_released_when_job_fails(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "ACCEPTED-FAIL-PROTECT")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    _persist_accepted_cell_attempt(session, job_id=job.job_id, step_id=step.job_step_id, req_id="accepted-fail-protect")
    # Restart reconciliation may turn an ambiguous accepted attempt into
    # UNKNOWN before failing the Job. The accepted timestamp remains the
    # physical-issue evidence; status alone must not make it releasable.
    ExecutionAttemptService(session).mark_unknown("accepted-fail-protect")

    orchestration.fail_job(job.job_id, reason="restart outcome is physically ambiguous")
    issued_ambiguous = session.get(Inventory, step.part_code)
    future_roof = session.get(Inventory, "ROOF_01")
    assert issued_ambiguous is not None and (issued_ambiguous.quantity, issued_ambiguous.reserved_quantity) == (20, 1)
    assert future_roof is not None and (future_roof.quantity, future_roof.reserved_quantity) == (20, 0)


def test_accepted_but_unconsumed_is_not_released_when_job_is_canceled(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "ACCEPTED-CANCEL-PROTECT")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    _persist_accepted_cell_attempt(session, job_id=job.job_id, step_id=step.job_step_id, req_id="accepted-cancel-protect")

    orchestration.cancel_job(job.job_id, reason="operator stop after accepted physical command")
    issued_ambiguous = session.get(Inventory, step.part_code)
    future_roof = session.get(Inventory, "ROOF_01")
    assert issued_ambiguous is not None and (issued_ambiguous.quantity, issued_ambiguous.reserved_quantity) == (20, 1)
    assert future_roof is not None and (future_roof.quantity, future_roof.reserved_quantity) == (20, 0)


def test_goal_rejected_does_not_consume_and_remains_normally_releasable(session: Session) -> None:
    product = _seed_recipe(session)
    job = _create(session, product, "REJECTED-NO-CONSUME")
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    step = orchestration.get_next_step(job.job_id)
    assert step is not None and step.part_code is not None
    attempts = ExecutionAttemptService(session)
    req_id = "rejected-before-accept"
    attempts.create_attempt(
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="GENERIC_INSTALL",
        request_payload={"job_step_id": step.job_step_id},
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        req_id=req_id,
    )
    attempts.mark_dispatching(req_id)
    attempts.apply_result(req_id, ExecutionAttemptStatus.FAILED, error_code="GOAL_REJECTED")

    inventory = session.get(Inventory, step.part_code)
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity) == (20, 1)
    orchestration.cancel_job(job.job_id, reason="safe rejected goal cleanup")
    inventory = session.get(Inventory, step.part_code)
    assert inventory is not None and (inventory.quantity, inventory.reserved_quantity) == (20, 0)
