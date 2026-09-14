from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.fake_cell_action_transport import FakeCellActionTransport
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, RobotCellTaskTypeMapper
from fms_server.robot_cell_slot_mapper import RobotCellSlotMapper
from scripts.seed_house_a_mvp_master import seed_house_a_current_master
from scripts.seed_house_b_mvp_master import seed_house_b_mvp_master
from shared.models import Base
from shared.models.factory import JobStep, RoofOptionCode
from shared.services.production_cell_payload_builder import ProductionCellPayloadBuilder
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import seed_inventory_for_recipe


HOUSE_A_WALLS = (
    ("INSTALL_DOOR_OUTER_WALL", "WALL-EXT-DOOR-01", "HOUSE_A_OUTER_WALL_DOOR_01", "blue", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_LEFT_OUTER_WALL", "WALL-EXT-LEFT-WINDOW-01", "HOUSE_A_OUTER_WALL_LEFT_01", "yellow", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_REAR_OUTER_WALL", "WALL-EXT-BACK-WINDOW-01", "HOUSE_A_OUTER_WALL_REAR_01", "red", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_RIGHT_OUTER_WALL", "WALL-EXT-RIGHT-01", "HOUSE_A_OUTER_WALL_RIGHT_01", "red_s", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_INNER_WALL", "WALL-INT-HOUSE-A-DOOR-01", "HOUSE_A_INNER_WALL_DOOR_01", "blue_in", "INSTALL_INNER_WALL", "wall_int", "WALL_INT_PALLET"),
    ("INSTALL_INNER_WALL", "WALL-INT-HOUSE-A-01", "HOUSE_A_INNER_WALL_01", "yellow_in", "INSTALL_INNER_WALL", "wall_int", "WALL_INT_PALLET"),
)

HOUSE_B_WALLS = (
    ("INSTALL_DOOR_OUTER_WALL", "WALL-EXT-DOOR-01", "HOUSE_B_OUTER_WALL_DOOR_01", "blue", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_LEFT_OUTER_WALL", "WALL-EXT-LEFT-WINDOW-01", "HOUSE_B_OUTER_WALL_LEFT_01", "yellow", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_REAR_OUTER_WALL", "WALL-EXT-BACK-WINDOW-01", "HOUSE_B_OUTER_WALL_REAR_01", "red", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_RIGHT_OUTER_WALL", "WALL-EXT-RIGHT-01", "HOUSE_B_OUTER_WALL_RIGHT_01", "red_s", "INSTALL_OUTER_WALL", "wall_ext", "WALL_EXT_PALLET"),
    ("INSTALL_INNER_WALL", "WALL-INT-HOUSE-B-01", "HOUSE_B_INNER_WALL_01", "red_in", "INSTALL_INNER_WALL", "wall_int", "WALL_INT_PALLET"),
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        recipe_a = seed_house_a_current_master(db)
        recipe_b = seed_house_b_mvp_master(db)
        seed_inventory_for_recipe(db, recipe_a)
        seed_inventory_for_recipe(db, recipe_b)
        db.commit()
        yield db
    Base.metadata.drop_all(engine)
    engine.dispose()


def _job(session: Session, *, product_code: str, roof: RoofOptionCode):
    return ProductionOrchestrationService(session).create_job(
        product_code=product_code,
        job_code=f"{product_code}-WIRE-CONTRACT",
        roof_option_code=roof,
    )


def _wall_rows(session: Session, *, job_id: int):
    builder = ProductionCellPayloadBuilder(session)
    steps = list(session.scalars(
        select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.step_order)
    ))
    return [
        (step, builder.build_for_job_step(job_id=job_id, job_step_id=step.job_step_id).parts[0])
        for step in steps
        if step.operation_code != "INSTALL_BASE"
    ]


@pytest.mark.parametrize(
    ("product_code", "roof", "expected"),
    [
        ("HOUSE_A", RoofOptionCode.ROOF_01, HOUSE_A_WALLS),
        ("HOUSE_B", RoofOptionCode.ROOF_02, HOUSE_B_WALLS),
    ],
)
def test_current_wall_recipes_preserve_canonical_slots_and_emit_final_wire_aliases(
    session: Session,
    product_code: str,
    roof: RoofOptionCode,
    expected: tuple[tuple[str, str, str, str, str, str, str], ...],
) -> None:
    job = _job(session, product_code=product_code, roof=roof)
    rows = _wall_rows(session, job_id=job.job_id)
    assert [
        (
            step.operation_code, step.part_code, step.slot_code, part.slot,
            RobotCellTaskTypeMapper.map_operation_code(step.operation_code),
            part.class_name, part.zone,
        )
        for step, part in rows
    ] == list(expected)


@pytest.mark.parametrize(
    ("product_code", "roof", "row_index", "expected_slot"),
    [
        ("HOUSE_A", RoofOptionCode.ROOF_01, 0, "blue"),
        ("HOUSE_A", RoofOptionCode.ROOF_01, 4, "blue_in"),
        ("HOUSE_B", RoofOptionCode.ROOF_02, 0, "blue"),
        ("HOUSE_B", RoofOptionCode.ROOF_02, 4, "red_in"),
    ],
)
def test_completed_json_correlates_against_final_wire_slot(
    session: Session,
    product_code: str,
    roof: RoofOptionCode,
    row_index: int,
    expected_slot: str,
) -> None:
    job = _job(session, product_code=product_code, roof=roof)
    step, part = _wall_rows(session, job_id=job.job_id)[row_index]
    assert part.slot == expected_slot
    adapter = RobotCellActionAdapter(FakeCellActionTransport([]))
    command = adapter.build_command(
        job=job,
        step=step,
        req_id=f"wire-{product_code}-{row_index}",
        parts_json=adapter.serialize_parts_payload((part,)),
    )
    adapter.validate_successful_result(
        command=command,
        result=CellTaskExecutionResult.success(completed_slots=(expected_slot,)),
    )
    assert step.slot_code != expected_slot


def test_historical_part_snapshot_keeps_its_canonical_slot() -> None:
    assert RobotCellSlotMapper.map(
        product_code="HOUSE_A",
        canonical_slot_code="HOUSE_A_OUTER_WALL_DOOR_01",
        part_code="WALL-EXT-DOOR-BLK01",
    ) == "HOUSE_A_OUTER_WALL_DOOR_01"
