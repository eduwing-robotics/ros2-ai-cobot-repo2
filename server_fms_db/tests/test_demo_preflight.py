from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import scripts.demo_preflight as preflight
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutorType,
    IncomingQATransaction,
    IncomingQATransactionStatus,
    Inventory,
    JobMaterialDelivery,
    JobStatus,
    MaterialDeliveryStatus,
    Product,
    ProductionJob,
    SupplyMode,
)
from shared.services.drop_resource_service import DropResourceState


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_B", product_name="House B"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def _settings(**overrides):
    values = {
        "vision_incoming_qa_udp_host": "127.0.0.1",
        "vision_incoming_qa_udp_port": 21001,
        "fms_incoming_qa_result_udp_host": "127.0.0.1",
        "fms_incoming_qa_result_udp_port": 21002,
        "incoming_qa_udp_ack_timeout_seconds": 2.0,
        "incoming_qa_udp_max_retries": 2,
        "vision_pre_roof_udp_host": "127.0.0.1",
        "vision_pre_roof_udp_port": 22001,
        "fms_pre_roof_result_udp_host": "127.0.0.1",
        "fms_pre_roof_result_udp_port": 22002,
        "cell_transport": "fake",
        "test_override_enabled": False,
        "material_prefetch_mode": "disabled",
        "postgres_test_database_url": "postgresql+psycopg://u:p@localhost:5432/smart_factory_benchmark",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _recipe(session: Session, groups=preflight.EXPECTED_HOUSE_B_GROUPS) -> AssemblyRecipe:
    recipe = AssemblyRecipe(product_code="HOUSE_B", version=1, is_active=True, description="test")
    session.add(recipe)
    session.flush()
    for index, group in enumerate(groups, 1):
        part = f"PART-{index}"
        # Reuse a part for the four outer stages only after creating its inventory.
        if group == "OUTER_WALLS":
            part = "PART-OUTER"
        session.add(AssemblyRecipeStage(
            recipe_id=recipe.recipe_id, stage_order=index, operation_code=f"OP-{index}",
            display_name=f"Stage {index}", part_code=None, quantity=1,
            supply_mode=SupplyMode.TRANSPORTED if group in {"OUTER_WALLS", "INNER_WALL"} else SupplyMode.MANUAL,
            supply_group_code=group, supply_destination_code="DROP" if group in {"OUTER_WALLS", "INNER_WALL"} else None,
            is_terminal=(group == "ROOF"),
        ))
    session.commit()
    return recipe


def _job(session: Session, status: JobStatus) -> ProductionJob:
    job = ProductionJob(job_code=f"JOB-{status.value}-{session.query(ProductionJob).count()}", product_code="HOUSE_B", status=status)
    session.add(job)
    session.flush()
    return job


def _occupied_delivery(session: Session, *, job_status: JobStatus = JobStatus.COMPLETED) -> tuple[ProductionJob, JobMaterialDelivery]:
    job = _job(session, job_status)
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"DEL-{job.job_id}",
        display_name="delivery", status=MaterialDeliveryStatus.COMPLETED,
        supply_mode=SupplyMode.TRANSPORTED, supply_group_code="OUTER_WALLS", supply_destination_code="DROP",
    )
    session.add(delivery)
    session.flush()
    session.add(ExecutionAttempt(
        req_id=f"forward-{delivery.job_delivery_id}", executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT", job_id=job.job_id, job_delivery_id=delivery.job_delivery_id,
        attempt_no=1, status=ExecutionAttemptStatus.SUCCEEDED,
        request_payload_json='{"pickup_code":"RACK1","dropoff_code":"DROP"}',
    ))
    session.commit()
    return job, delivery


def test_database_identity_requires_verified_benchmark() -> None:
    good = make_url("postgresql+psycopg://u:p@localhost:5432/smart_factory_benchmark")
    assert preflight.database_identity_check(url=good, actual_database="smart_factory_benchmark").passed
    assert not preflight.database_identity_check(url=good, actual_database="smart_factory_db").passed
    bad = make_url("postgresql+psycopg://u:p@localhost:5432/smart_factory_db")
    assert not preflight.database_identity_check(url=bad).passed


def test_no_active_job_passes_and_active_job_fails(session: Session) -> None:
    assert preflight._check_jobs(session).passed
    _job(session, JobStatus.RUNNING)
    session.commit()
    check = preflight._check_jobs(session)
    assert not check.passed and "status=RUNNING" in check.details[0]


def test_terminal_parent_stale_qa_does_not_block_but_live_qa_does(session: Session) -> None:
    terminal = _job(session, JobStatus.CANCELED)
    session.add(IncomingQATransaction(
        inspection_request_id="terminal-acked", production_job_id=terminal.job_id,
        inspection_mode="HOUSE_B", inspection_cycle=1, status=IncomingQATransactionStatus.ACKED,
        immutable_request_snapshot="{}",
    ))
    session.commit()
    assert preflight._check_incoming_qa(session).passed
    live = _job(session, JobStatus.RUNNING)
    session.add(IncomingQATransaction(
        inspection_request_id="live-acked", production_job_id=live.job_id,
        inspection_mode="HOUSE_B", inspection_cycle=1, status=IncomingQATransactionStatus.ACKED,
        immutable_request_snapshot="{}",
    ))
    session.commit()
    assert not preflight._check_incoming_qa(session).passed


def test_drop_free_passes_and_occupied_fails(session: Session) -> None:
    assert preflight._check_drop(session).passed
    _, delivery = _occupied_delivery(session)
    check = preflight._check_drop(session)
    assert not check.passed
    assert check.data["owner_delivery_id"] == delivery.job_delivery_id


def test_active_attempt_fails(session: Session) -> None:
    job = _job(session, JobStatus.RUNNING)
    session.add(ExecutionAttempt(
        req_id="active", executor_type=ExecutorType.ROBOT_CELL, command_type="EXECUTE_TASK",
        job_id=job.job_id, attempt_no=1, status=ExecutionAttemptStatus.ACCEPTED,
        request_payload_json="{}",
    ))
    session.commit()
    assert not preflight._check_attempts(session).passed


def test_recipe_order_and_inventory_validation(session: Session) -> None:
    recipe = _recipe(session)
    recipe_check, stages = preflight.recipe_check(session)
    assert recipe_check.passed
    for stage in stages:
        if stage.part_code:
            session.add(Inventory(part_code=stage.part_code, quantity=3, reserved_quantity=1))
    # The fixture recipe intentionally has no part requirements; add one to validate inventory arithmetic.
    stages[0].part_code = "BASE-PART"
    session.add(Inventory(part_code="BASE-PART", quantity=1, reserved_quantity=0))
    session.commit()
    assert preflight.inventory_check(session, stages).passed
    session.get(Inventory, "BASE-PART").quantity = 0
    session.commit()
    assert not preflight.inventory_check(session, stages).passed


def test_wrong_recipe_order_fails(session: Session) -> None:
    _recipe(session, ("BASE", "INNER_WALL", "OUTER_WALLS", "OUTER_WALLS", "OUTER_WALLS", "OUTER_WALLS", "ROOF"))
    assert not preflight.recipe_check(session)[0].passed


def test_config_validation_and_warning() -> None:
    check, warnings = preflight.config_check(_settings(test_override_enabled=True))
    assert check.passed and "WARNING: TEST_OVERRIDE_ENABLED=true" in warnings
    failed, _ = preflight.config_check(_settings(vision_incoming_qa_udp_port=70000))
    assert not failed.passed and "VISION_INCOMING_QA_UDP_PORT" in failed.details[0]


def test_report_exit_code_only_zero_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    ready = preflight.PreflightReport({"database": preflight.Check(True)}, [])
    not_ready = preflight.PreflightReport({"database": preflight.Check(False)}, [])
    monkeypatch.setattr(preflight, "run_preflight", lambda: ready)
    assert preflight.main(["--json"]) == 0
    monkeypatch.setattr(preflight, "run_preflight", lambda: not_ready)
    assert preflight.main(["--json"]) == 1


def test_checks_do_not_mutate_domain_rows(session: Session) -> None:
    job, delivery = _occupied_delivery(session)
    before = (job.status, delivery.status, session.query(ExecutionAttempt).count())
    preflight._check_jobs(session)
    preflight._check_incoming_qa(session)
    preflight._check_drop(session)
    preflight._check_attempts(session)
    assert (session.get(ProductionJob, job.job_id).status, session.get(JobMaterialDelivery, delivery.job_delivery_id).status, session.query(ExecutionAttempt).count()) == before
