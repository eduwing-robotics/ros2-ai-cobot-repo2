from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from shared.models.factory import JobStatus, Product, RoofOptionCode
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pre_roof_actual_vision_e2e", ROOT / "scripts" / "pre_roof_actual_vision_e2e.py"
)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    value = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with value() as session:
            session.add(Product(product_code="ACTUAL_E2E", product_name="Actual E2E"))
            session.flush()
            add_gated_roof_stages(session, add_active_recipe(session, "ACTUAL_E2E", stages=HOUSE_B_STAGES))
            session.commit()
        yield value
    finally:
        engine.dispose()


def pre_roof_job(factory, *, code="ACTUAL-E2E"):
    with factory() as session:
        service = ProductionOrchestrationService(session)
        job = service.create_job(product_code="ACTUAL_E2E", job_code=code, roof_option_code=RoofOptionCode.ROOF_02)
        service.start_job(job.job_id)
        while (step := service.get_next_step(job.job_id)) is not None:
            service.start_step(step.job_step_id)
            service.complete_step(step.job_step_id)
        session.expire_all()
        return session.get(type(job), job.job_id)


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://127.0.0.1/smart_factory_db",
    "postgresql+psycopg://127.0.0.1/smart_factory_benchmark",
    "postgresql+psycopg://127.0.0.1/another_db",
    "sqlite:///smart_factory_rehearsal.db",
    "postgresql+psycopg://192.168.20.20/smart_factory_rehearsal",
])
def test_actual_harness_rejects_every_non_rehearsal_target(url):
    with pytest.raises(harness.ActualVisionHarnessError, match="rehearsal|PostgreSQL|loopback"):
        harness.actual_child_environment({"FACTORY_REHEARSAL_DATABASE_URL": url})


def test_actual_harness_child_environment_is_exact_and_hardware_free():
    rehearsal_url = "postgresql+psycopg://127.0.0.1:5432/smart_factory_rehearsal"
    env = harness.actual_child_environment({
        "FACTORY_REHEARSAL_DATABASE_URL": rehearsal_url,
        "DATABASE_URL": "postgresql+psycopg://127.0.0.1/smart_factory_db",
        "VISION_PRE_ROOF_UDP_HOST": "127.0.0.1",
        "VISION_INCOMING_QA_UDP_HOST": "192.168.20.30",
        "ROS_DOMAIN_ID": "73", "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp",
    })
    assert env["DATABASE_URL"] == rehearsal_url
    assert env["CELL_TRANSPORT"] == "fake"
    assert env["MATERIAL_PREFETCH_MODE"] == "disabled"
    assert env["TELEMETRY_ROS_ENABLED"] == "false"
    assert env["VISION_PRE_ROOF_UDP_HOST"] == "192.168.20.30"
    assert env["VISION_PRE_ROOF_UDP_PORT"] == "20061"
    assert env["FMS_PRE_ROOF_RESULT_UDP_HOST"] == "192.168.20.20"
    assert env["FMS_PRE_ROOF_RESULT_UDP_BIND_HOST"] == "0.0.0.0"
    assert env["FMS_PRE_ROOF_RESULT_UDP_PORT"] == "20062"
    for key in ("VISION_INCOMING_QA_UDP_HOST", "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION"):
        assert key not in env


def test_actual_harness_owns_only_api_and_fms_processes():
    commands = harness.actual_child_commands()
    assert commands == (
        (str(harness.PYTHON), "-m", "uvicorn", "api_server.main:app", "--host", "0.0.0.0", "--port", "8000"),
        (str(harness.PYTHON), "-m", "fms_server.main"),
    )
    rendered = " ".join(" ".join(command).lower() for command in commands)
    for forbidden in ("fake_vision", "fake_cell", "telemetry", "voice", "ros2", "turtlebot", "fr5", "zkbot"):
        assert forbidden not in rendered


def test_actual_harness_port_preflight_checks_api_result_and_local_status(monkeypatch):
    calls = []
    monkeypatch.setattr(harness, "_assert_port_free", lambda kind, host, port: calls.append((kind, host, port)))
    harness.assert_actual_ports_available()
    assert calls == [
        (harness.socket.SOCK_STREAM, "0.0.0.0", 8000),
        (harness.socket.SOCK_DGRAM, "0.0.0.0", 20062),
        (harness.socket.SOCK_DGRAM, "127.0.0.1", 20050),
    ]


def test_pending_pre_roof_job_is_initial_start_safe(factory):
    job = pre_roof_job(factory)
    assert harness.verify_actual_vision_job(factory, job_id=job.job_id) == (job.job_id, job.job_code, "INITIAL_START")


def test_completed_nonvalid_job_is_reinspection_safe(factory):
    job = pre_roof_job(factory)
    with factory() as session:
        completion = ProductionCompletionService(session)
        completion.start_pre_roof_inspection(production_job_id=job.job_id)
        completion.fail_pre_roof_inspection(production_job_id=job.job_id)
    assert harness.verify_actual_vision_job(factory, job_id=job.job_id) == (job.job_id, job.job_code, "REINSPECTION")


def test_running_job_and_extra_active_job_fail_closed(factory):
    job = pre_roof_job(factory)
    with factory() as session:
        ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
    with pytest.raises(harness.ActualVisionHarnessError) as running:
        harness.verify_actual_vision_job(factory, job_id=job.job_id)
    assert running.value.phase == "JOB_RUNNING"

    # Fresh isolated fixture state: a second active Job blocks global FMS scan.
    with factory() as session:
        other = ProductionOrchestrationService(session).create_job(
            product_code="ACTUAL_E2E", job_code="SECOND", roof_option_code=RoofOptionCode.ROOF_02
        )
        assert other.status is JobStatus.REQUESTED
    with pytest.raises(harness.ActualVisionHarnessError) as extra:
        harness.verify_actual_vision_job(factory, job_id=job.job_id)
    assert extra.value.phase == "EXTRA_ACTIVE_JOB"


def test_monitor_controls_follow_canonical_wire_authority():
    page = (ROOT / "api_server/static/production_monitor.html").read_text()
    assert "inspection.status === 'PENDING'" in page
    assert "품질검사 시작" in page
    assert "inspection.status === 'RUNNING'" in page
    assert "Vision View 검사 진행 중입니다." in page
    assert "inspection.status === 'COMPLETED' && inspection.result !== 'PASS'" in page
    assert "재검사 시작" in page
    assert "runPreRoofCommand(${escapeHtml(snapshot.job_id)}, 'pass')" not in page
    assert "runPreRoofCommand(${escapeHtml(snapshot.job_id)}, 'fail')" not in page



def test_actual_harness_source_never_auto_starts_or_launches_fake_vision():
    source = (ROOT / "scripts/pre_roof_actual_vision_e2e.py").read_text()
    assert "post_start(" not in source
    assert "fake_vision" not in source.lower()
    assert "fake_cell" not in source.lower()
    assert "pre_roof_actual_vision_e2e" in source


def test_actual_harness_reuses_factory_stack_fail_closed_guard(monkeypatch):
    called = []
    monkeypatch.setattr(harness, "verify_factory_not_running", lambda: called.append(True))
    harness.verify_factory_not_running()
    assert called == [True]
