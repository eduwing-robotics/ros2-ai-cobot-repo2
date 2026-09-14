from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from fms_server.cell_action_transport import CellTaskCommand, CellTaskExecutionResult
from fms_server.execution_coordinator import CoordinatorOutcome
from fms_server.robot_cell_action_adapter import RobotCellTaskTypeMapper
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from scripts import run_fake_production_demo as demo
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    JobMaterialDelivery,
    JobStatus,
    JobStep,
    Part,
    PartCategory,
    Product,
    ProductionInspection,
    ProductionJob,
    ProductionInspectionStatus,
    StepStatus,
)
from shared.services.production_execution_snapshot_service import ProductionExecutionSnapshotService
from tests.recipe_test_support import HOUSE_A_STAGES, HOUSE_B_STAGES, add_active_recipe, seed_inventory_for_recipe


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all([
        Product(product_code="HOUSE_A", product_name="A형 주택"),
        Product(product_code="HOUSE_B", product_name="B형 주택"),
    ])
    db.flush()
    add_active_recipe(db, "HOUSE_A", stages=HOUSE_A_STAGES)
    add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES)
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("product_code", ["HOUSE_A", "HOUSE_B"])
def test_fake_demo_runs_active_recipe_through_coordinator_to_pre_roof_ready(
    session: Session,
    product_code: str,
) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == product_code))
    assert recipe is not None
    expected_operations = [stage.operation_code for stage in sorted(recipe.stages, key=lambda stage: stage.stage_order)]

    result = demo.run_fake_production_demo(session, product_code=product_code, delay_seconds=0)

    job = session.get(ProductionJob, result.job_id)
    steps = list(
        session.scalars(
            select(JobStep)
            .where(JobStep.job_id == result.job_id, JobStep.source_recipe_stage_id.is_not(None))
            .order_by(JobStep.step_order)
        )
    )
    inspection = session.scalar(
        select(ProductionInspection).where(ProductionInspection.production_job_id == result.job_id)
    )
    snapshot = ProductionExecutionSnapshotService(session).get_snapshot(job_id=result.job_id)

    assert result.assembly_recipe_id == recipe.recipe_id
    assert result.assembly_recipe_version == recipe.version
    assert result.recipe_stage_count == len(expected_operations)
    if product_code in {"HOUSE_A", "HOUSE_B"}:
        # The fake demo intentionally does not fabricate authoritative Incoming QA
        # release evidence.  Every material-bearing product must stop safely before execution.
        assert result.stopped_outcome is CoordinatorOutcome.NOT_DISPATCHED_NOT_READY
        assert result.action_commands == ()
        assert all(step.status is StepStatus.PENDING for step in steps)
        assert job is not None and job.status is JobStatus.RUNNING
        assert inspection is None
        assert snapshot.job_status is JobStatus.RUNNING
        assert snapshot.next_step is not None
        assert snapshot.next_step.readiness_reason == "PRE_PRODUCTION_QA_INCOMPLETE"
        return
    assert [command.task_type for command in result.action_commands] == [RobotCellTaskTypeMapper.map_operation_code(operation) for operation in expected_operations[:-1]]
    assert all(command.product == product_code for command in result.action_commands)
    assert all(command.req_id == f"demo-job-{result.job_id}-step-{command.step_id}" for command in result.action_commands)
    assert all(step.source_recipe_stage_id is not None for step in steps)
    assert all(step.status is StepStatus.COMPLETED for step in steps[:-1])
    assert steps[-1].status is StepStatus.PENDING
    assert job is not None and job.status is JobStatus.RUNNING
    assert inspection is None
    assert snapshot.job_status is JobStatus.RUNNING
    assert snapshot.current_step is None and snapshot.next_step is not None
    assert snapshot.next_step.readiness_reason == "INCOMING_QA_HOLD"
    assert snapshot.inspection is None


def test_fake_demo_stops_before_dispatch_when_material_is_not_ready(session: Session) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None
    part = Part(vision_class="wall_ext_back", part_code="TEST_PART", part_name="Test Part", category=PartCategory.STRUCTURE, unit="EA")
    session.add(part)
    session.flush()
    stage = session.scalar(select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id).order_by(AssemblyRecipeStage.stage_order))
    stage.part_code = part.part_code
    stage.quantity = 1
    seed_inventory_for_recipe(session, recipe)
    session.flush()

    result = demo.run_fake_production_demo(session, product_code="HOUSE_A", delay_seconds=0, auto_satisfy_material=False)
    job = session.get(ProductionJob, result.job_id)
    step = session.scalar(
        select(JobStep).where(JobStep.job_id == result.job_id).order_by(JobStep.step_order)
    )
    delivery = session.scalar(
        select(JobMaterialDelivery).where(JobMaterialDelivery.production_job_id == result.job_id)
    )

    assert result.stopped_outcome is CoordinatorOutcome.NOT_DISPATCHED_NOT_READY
    assert result.action_commands == ()
    assert step is not None and step.status is StepStatus.PENDING
    assert delivery is not None
    assert job is not None and job.status is JobStatus.RUNNING


def test_cli_arguments_and_source_guard() -> None:
    parser = demo.build_parser()
    defaults = parser.parse_args([])
    assert defaults.product == "HOUSE_A"
    assert defaults.delay == 2.0
    with pytest.raises(SystemExit):
        parser.parse_args(["--delay", "-0.1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--product", "HOUSE_X"])
    assert "--product" in parser.format_help()
    assert "--delay" in parser.format_help()

    source = (demo.Path(demo.__file__)).read_text()
    assert ".status =" not in source
    assert "orchestration.start_step(" not in source
    assert "orchestration.complete_step(" not in source


def test_fake_transport_delay_occurs_after_goal_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    timeline: list[str] = []
    monkeypatch.setattr("fms_server.fake_cell_action_transport.sleep", lambda seconds: timeline.append(f"sleep:{seconds}"))
    transport = FakeCellActionTransport([
        FakeCellActionExchange(CellTaskExecutionResult.success(), execution_delay_seconds=0.25)
    ])
    command = CellTaskCommand(
        ver="0.2", req_id="demo-delay", job_id="JOB-1", step_id="STEP-2",
        task_type="INSTALL_FLOOR", product="HOUSE_A", parts_json='[{"slot":"S","class":"base"}]',
    )

    result = transport.execute(command, goal_accepted_callback=lambda: timeline.append("accepted"))

    assert result.succeeded is True
    assert timeline == ["accepted", "sleep:0.25"]
