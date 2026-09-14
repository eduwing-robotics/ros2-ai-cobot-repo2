from __future__ import annotations

import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pre_roof_full_stack_rehearsal", ROOT / "scripts" / "pre_roof_full_stack_rehearsal.py"
)
assert SPEC and SPEC.loader
rehearsal = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rehearsal
SPEC.loader.exec_module(rehearsal)


def test_loopback_child_environment_overrides_only_child_values() -> None:
    env = rehearsal.safe_child_environment(
        {
            "POSTGRES_TEST_DATABASE_URL": "postgresql://127.0.0.1:5432/smart_factory_benchmark",
            "VISION_INCOMING_QA_UDP_HOST": "192.168.20.30",
            "FMS_INCOMING_QA_RESULT_UDP_HOST": "192.168.20.20",
        }
    )

    assert env["DATABASE_URL"] == env["POSTGRES_TEST_DATABASE_URL"]
    assert env["VISION_PRE_ROOF_UDP_HOST"] == "127.0.0.1"
    assert env["FMS_PRE_ROOF_RESULT_UDP_HOST"] == "127.0.0.1"
    assert env["FMS_PRE_ROOF_RESULT_UDP_BIND_HOST"] == "127.0.0.1"
    assert env["FMS_PRE_ROOF_RESULT_UDP_PORT"] == "20062"
    assert "VISION_INCOMING_QA_UDP_HOST" not in env
    assert "FMS_INCOMING_QA_RESULT_UDP_HOST" not in env


def test_loopback_predicate_rejects_actual_vision_hosts() -> None:
    assert rehearsal.loopback_host("127.0.0.1")
    assert rehearsal.loopback_host("localhost")
    assert not rehearsal.loopback_host("192.168.20.30")
    assert not rehearsal.loopback_host("192.168.20.20")


def test_source_preflight_accepts_current_removed_public_bypasses() -> None:
    rehearsal.verify_source_bypasses_removed()


def test_child_environment_requires_dedicated_benchmark_url() -> None:
    with pytest.raises(rehearsal.RehearsalError, match="POSTGRES_TEST_DATABASE_URL"):
        rehearsal.safe_child_environment({})


def test_rehearsal_database_target_injects_only_the_explicit_allowlisted_url() -> None:
    rehearsal_url = "postgresql+psycopg://127.0.0.1:5432/smart_factory_rehearsal"
    env = rehearsal.safe_child_environment(
        {"FACTORY_REHEARSAL_DATABASE_URL": rehearsal_url}, database_target="rehearsal"
    )

    assert env["DATABASE_URL"] == rehearsal_url
    assert env["FMS_PRE_ROOF_RESULT_UDP_HOST"] == "127.0.0.1"


def test_runner_rejects_misnamed_database_url_before_connection() -> None:
    with pytest.raises(rehearsal.RehearsalError, match="no connection was opened"):
        rehearsal.rehearsal_session_factory(
            {"DATABASE_URL": "postgresql+psycopg://127.0.0.1/smart_factory_db"},
            expected_database="smart_factory_rehearsal",
        )


def test_start_request_matches_registered_openapi_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from api_server.main import app

    captured = []

    def fake_urlopen(request, *, timeout):
        captured.append(request)
        response = BytesIO(b'{"status":"RUNNING"}')
        response.status = 200
        return response

    monkeypatch.setattr(rehearsal.urllib.request, "urlopen", fake_urlopen)
    assert rehearsal.post_start(1) == {"status": "RUNNING"}
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "http://127.0.0.1:8000/production/jobs/1/pre-roof/start"
    assert request.get_method() == "POST"
    assert request.data == b"{}"
    route_path = urlsplit(request.full_url).path.replace("/jobs/1/", "/jobs/{job_id}/")
    assert "post" in app.openapi()["paths"][route_path]


@pytest.mark.parametrize("path", [
    "/api/v1/production/jobs/1/pre-roof/start",
    "/production/jobs/1/pre-roof/pass",
    "/api/v1/production/jobs/1/pre-roof/pass",
    "/api/v1/incoming-qa/results",
    "/incoming-qa/results",
])
def test_stale_and_bypass_routes_remain_unavailable(path: str) -> None:
    from api_server.main import app
    from fastapi.testclient import TestClient

    # No lifespan startup: route lookups need no DB, Redis, or UDP runtime.
    client = TestClient(app)
    try:
        response = client.post(path, json={
            "status": "COMPLETED", "result": "PASS", "production_valid": True,
        })
        assert response.status_code == 404
    finally:
        client.close()


@pytest.fixture
def running_unsent_job():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from shared.models import Base
    from shared.models.factory import Product, RoofOptionCode
    from shared.services.production_orchestration_service import ProductionOrchestrationService
    from shared.services.production_completion_service import ProductionCompletionService
    from tests.recipe_test_support import HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            session.add(Product(product_code="RESUME", product_name="Resume test"))
            session.flush()
            add_gated_roof_stages(session, add_active_recipe(session, "RESUME", stages=HOUSE_B_STAGES))
            session.commit()
            service = ProductionOrchestrationService(session)
            job = service.create_job(product_code="RESUME", job_code="RESUME-JOB", roof_option_code=RoofOptionCode.ROOF_02)
            service.start_job(job.job_id)
            while (step := service.get_next_step(job.job_id)) is not None:
                service.start_step(step.job_step_id)
                service.complete_step(step.job_step_id)
            inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
        yield factory, job, inspection
    finally:
        engine.dispose()


def test_explicit_resume_accepts_only_running_unsent_and_preserves_identity(running_unsent_job):
    from shared.services.production_completion_service import ProductionCompletionService, InvalidProductionCompletionTransitionError
    factory, job, inspection = running_unsent_job
    with pytest.raises(rehearsal.RehearsalError, match="resume-unsent"):
        rehearsal.verify_job_is_safe(factory, job.job_id)
    assert rehearsal.verify_job_is_safe(factory, job.job_id, resume_unsent=True) == (job.job_id, job.job_code)
    with factory() as session:
        with pytest.raises(InvalidProductionCompletionTransitionError):
            ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
    current = rehearsal.latest_projection(factory, job.job_id)["inspection"]
    assert current["inspection_id"] == inspection.inspection_id
    assert current["inspection_request_id"] == inspection.inspection_request_id
    assert current["inspection_cycle"] == 1 and current["status"] == "RUNNING"


@pytest.mark.parametrize("field,value", [
    ("wire_request_snapshot_json", "{}"), ("wire_request_digest", "a" * 64),
    ("wire_result_digest", "b" * 64), ("wire_retry_count", 1),
    ("wire_error_code", "UDP_SEND_FAILED"),
    ("wire_sent_at", "timestamp"), ("wire_acked_at", "timestamp"),
])
def test_resume_refuses_any_persisted_transport_evidence(running_unsent_job, field, value):
    from datetime import datetime, timezone
    from shared.models.factory import ProductionInspection
    factory, job, inspection = running_unsent_job
    with factory() as session:
        row = session.get(ProductionInspection, inspection.inspection_id)
        setattr(row, field, datetime.now(timezone.utc) if value == "timestamp" else value)
        session.commit()
    with pytest.raises(rehearsal.RehearsalError) as raised:
        rehearsal.verify_job_is_safe(factory, job.job_id, resume_unsent=True)
    assert raised.value.phase == "RESUME_UNSAFE"


def test_resume_observer_skips_start_and_subscribes_before_fms(monkeypatch):
    import asyncio
    import json
    import websockets
    events = []
    active = {"job_id": 1, "inspection_type": "PRE_ROOF", "inspection": {
        "inspection_id": 9, "inspection_request_id": "same-request", "inspection_cycle": 1, "status": "RUNNING",
    }}
    final = {**active, "inspection": {**active["inspection"], "status": "COMPLETED", "transport": {"acked": True}}}

    class Connection:
        def __init__(self, messages): self.messages = iter(messages)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def recv(self):
            message = next(self.messages)
            events.append(message["type"])
            return json.dumps(message)

    connections = iter([
        Connection([{"type": "production_snapshot", "data": {"production_inspections": [active]}},
                    {"type": "production_inspection_status", "data": final}]),
        Connection([{"type": "production_snapshot", "data": {"production_inspections": [final]}}]),
    ])
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: next(connections))
    def forbidden_start(*args): raise AssertionError("resume must not POST start")
    monkeypatch.setattr(rehearsal, "post_start", forbidden_start)
    response, realtime, snapshot = asyncio.run(rehearsal.observe_realtime_and_snapshot(
        1, timeout=1, resume_inspection_id=9, start_fms=lambda: events.append("FMS_START"),
    ))
    assert events == ["production_snapshot", "FMS_START", "production_inspection_status", "production_snapshot"]
    assert response["resumed"] is True and response["inspection_id"] == 9
    assert realtime == snapshot == final


@pytest.mark.parametrize("alive,status,sent,acked,phase", [
    (False, "RUNNING", None, None, "FMS_PROCESS_EXITED"),
    (True, "RUNNING", None, None, "REQUEST_NOT_SENT"),
    (True, "RUNNING", "sent", None, "ACK_NOT_APPLIED"),
    (True, "RUNNING", "sent", "acked", "RESULT_NOT_APPLIED"),
    (True, "ERROR", None, None, "RESULT_NOT_APPLIED"),
    (True, "COMPLETED", "sent", "acked", "UNITY_REALTIME_TIMEOUT"),
])
def test_timeout_diagnostics_identify_upstream_phase(monkeypatch, alive, status, sent, acked, phase):
    from types import SimpleNamespace
    projection = {"inspection": {"status": status, "transport": {
        "request_sent_at": sent, "acked_at": acked, "acked": acked is not None,
        "wire_error_code": "ERROR_HINT" if status == "ERROR" else None,
    }}}
    monkeypatch.setattr(rehearsal, "latest_projection", lambda *args: projection)
    error = rehearsal.realtime_timeout_diagnostic(None, 1, SimpleNamespace(poll=lambda: None if alive else 1), Path("fms.log"))
    assert error.phase == phase
    assert f"inspection_status={status}" in str(error)
    assert f"wire_sent_at={sent}" in str(error)
    assert "FMS log=fms.log" in str(error)


def test_ack_metadata_wait_polls_until_committed(monkeypatch):
    def projection(acked):
        return {"inspection": {"inspection_id": 9, "status": "COMPLETED", "transport": {
            "acked": acked, "acked_at": "persisted" if acked else None, "wire_error_code": None}}}
    reads = iter([projection(False), projection(False), projection(True)])
    monkeypatch.setattr(rehearsal, "latest_projection", lambda *a: next(reads))
    monkeypatch.setattr(rehearsal.time, "sleep", lambda *a: None)
    assert rehearsal.wait_for_ack_projection(None, 1, inspection_id=9, timeout=1)["inspection"]["transport"]["acked"]


def test_ack_metadata_genuinely_absent_still_fails(monkeypatch):
    monkeypatch.setattr(rehearsal, "latest_projection", lambda *a: {
        "inspection": {"inspection_id": 9, "status": "COMPLETED", "transport": {"acked": False}}})
    ticks = iter([0, 0, 4])
    monkeypatch.setattr(rehearsal.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rehearsal.time, "sleep", lambda *a: None)
    with pytest.raises(rehearsal.RehearsalError) as raised:
        rehearsal.wait_for_ack_projection(None, 1, inspection_id=9, timeout=3)
    assert raised.value.phase == "ACK_NOT_APPLIED"


def test_observer_waits_for_late_ack_terminal_event_before_reconnect(monkeypatch):
    import asyncio
    import json
    import websockets
    events = []
    early = {"job_id": 1, "inspection_type": "PRE_ROOF", "inspection": {
        "inspection_id": 9, "status": "COMPLETED", "transport": {"acked": False}}}
    final = {**early, "inspection": {**early["inspection"], "transport": {"acked": True}}}
    class Connection:
        def __init__(self, messages): self.messages = iter(messages)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append("disconnect")
        async def recv(self): return json.dumps(next(self.messages))
    connections = iter([
        Connection([{"type": "production_snapshot", "data": {}},
                    {"type": "production_inspection_status", "data": early},
                    {"type": "production_inspection_status", "data": final}]),
        Connection([{"type": "production_snapshot", "data": {"production_inspections": [final]}}]),
    ])
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: next(connections))
    monkeypatch.setattr(rehearsal, "post_start", lambda *a: {})
    _, realtime, snapshot = asyncio.run(rehearsal.observe_realtime_and_snapshot(
        1, timeout=1, ack_wait_seconds=1, wait_for_ack=lambda inspection_id: events.append("ACK_COMMITTED")))
    assert realtime == snapshot == final
    assert events == ["ACK_COMMITTED", "disconnect", "disconnect"]
