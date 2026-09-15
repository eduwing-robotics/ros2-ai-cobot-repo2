#!/usr/bin/env python3
"""Run a guarded, loopback-only PRE_ROOF process-boundary rehearsal.

This tool deliberately does *not* create or mutate a job outside normal API/FMS
authority. Supply an already ``PRE_ROOF_READY`` Job in the selected allowlisted database with ``--job-id``.
It never runs Alembic and accepts only the explicit allowlist
``smart_factory_benchmark`` or ``smart_factory_rehearsal``.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from api_server.services.unity_production_inspection_projection_service import (
    UnityProductionInspectionProjectionService,
)
from shared.models.factory import (
    JobStatus,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionInspectionType,
    ProductionJob,
)

PYTHON = ROOT / ".venv" / "bin" / "python"
BENCHMARK_DATABASE = "smart_factory_benchmark"
REHEARSAL_DATABASE = "smart_factory_rehearsal"
EXPECTED_ALEMBIC_REVISION = "20260907_02"
API_PORT = 8000
VISION_REQUEST_PORT = 20061
FMS_RESULT_PORT = 20062
VISION_STATUS_PORT = 20050
SCENARIOS = (
    "all-pass-valid-false",
    "fail",
    "not-evaluated-runtime",
    "not-evaluated-error",
)


class RehearsalError(RuntimeError):
    """A human-readable rehearsal phase rather than a wire error code."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(detail)
        self.phase = phase


@dataclass
class RehearsalState:
    database: str = ""
    revision: str = ""
    job_id: int | None = None
    job_code: str | None = None
    request: dict[str, Any] | None = None
    realtime: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    inspection: dict[str, Any] | None = None
    fake_log: Path | None = None
    api_log: Path | None = None
    fms_log: Path | None = None


@dataclass
class OwnedProcesses:
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def start(self, command: list[str], *, env: Mapping[str, str], log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("wb") as stream:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=dict(env),
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        return process

    def stop(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def load_dotenv_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load existing local .env without exposing credentials in output."""
    result = subprocess.run(
        ["bash", "-lc", "set -a; [ -f .env ] && source .env; set +a; env -0"],
        cwd=ROOT,
        env=dict(base or os.environ),
        capture_output=True,
        check=True,
    )
    environment: dict[str, str] = {}
    for entry in result.stdout.split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            environment[key.decode()] = value.decode(errors="surrogateescape")
    return environment


def loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def safe_child_environment(
    parent: Mapping[str, str], *, database_target: str = "benchmark"
) -> dict[str, str]:
    """Per-child loopback settings only; never modify the developer's .env."""
    target_keys = {
        "benchmark": "POSTGRES_TEST_DATABASE_URL",
        "rehearsal": "FACTORY_REHEARSAL_DATABASE_URL",
    }
    target_key = target_keys.get(database_target)
    if target_key is None:
        raise RehearsalError("DB_PRECHECK_FAILED", "database target must be benchmark or rehearsal.")
    target_url = parent.get(target_key, "").strip()
    if not target_url:
        raise RehearsalError("DB_PRECHECK_FAILED", f"{target_key} is required.")
    env = dict(parent)
    env.update(
        {
            "DATABASE_URL": target_url,
            "CELL_TRANSPORT": "fake",
            "MATERIAL_PREFETCH_MODE": "disabled",
            "TELEMETRY_ROS_ENABLED": "false",
            "API_HOST": "127.0.0.1",
            "API_PORT": str(API_PORT),
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "VISION_STATUS_UDP_HOST": "127.0.0.1",
            "VISION_STATUS_UDP_PORT": str(VISION_STATUS_PORT),
            "VISION_PRE_ROOF_UDP_HOST": "127.0.0.1",
            "VISION_PRE_ROOF_UDP_PORT": str(VISION_REQUEST_PORT),
            "FMS_PRE_ROOF_RESULT_UDP_HOST": "127.0.0.1",
            "FMS_PRE_ROOF_RESULT_UDP_BIND_HOST": "127.0.0.1",
            "FMS_PRE_ROOF_RESULT_UDP_PORT": str(FMS_RESULT_PORT),
            "PRE_ROOF_UDP_ACK_TIMEOUT_SECONDS": "1",
            "PRE_ROOF_UDP_MAX_RETRIES": "2",
        }
    )
    # Do not accidentally start the Incoming QA runtime using physical Vision
    # addresses inherited from .env.
    for key in (
        "VISION_INCOMING_QA_UDP_HOST", "VISION_INCOMING_QA_UDP_PORT",
        "FMS_INCOMING_QA_RESULT_UDP_HOST", "FMS_INCOMING_QA_RESULT_UDP_PORT",
        "INCOMING_QA_UDP_ACK_TIMEOUT_SECONDS", "INCOMING_QA_UDP_MAX_RETRIES",
    ):
        env.pop(key, None)
    return env


def verify_source_bypasses_removed() -> None:
    """Fail closed if either retired public quality-authority bypass returns."""
    main_source = (ROOT / "api_server" / "main.py").read_text()
    incoming_source = (ROOT / "api_server" / "routers" / "incoming_qa.py").read_text()
    production_source = (ROOT / "api_server" / "routers" / "production.py").read_text()
    if "include_router(incoming_qa_router" in main_source or '@router.post("/results"' in incoming_source:
        raise RehearsalError("LEGACY_BYPASS_PRESENT", "Legacy Incoming QA HTTP result route is still registered.")
    if '"/jobs/{job_id}/pre-roof/pass"' in production_source:
        raise RehearsalError("PRE_ROOF_BYPASS_PRESENT", "Public PRE_ROOF trusted PASS route is still present.")


def rehearsal_session_factory(
    environment: Mapping[str, str], *, expected_database: str
) -> tuple[sessionmaker[Session], Any, RehearsalState]:
    if expected_database not in {BENCHMARK_DATABASE, REHEARSAL_DATABASE}:
        raise RehearsalError("DB_PRECHECK_FAILED", "Unsupported rehearsal database target.")
    url = environment.get("DATABASE_URL", "").strip()
    if not url:
        raise RehearsalError("DB_PRECHECK_FAILED", "Explicit child DATABASE_URL is empty.")
    try:
        parsed = make_url(url)
    except Exception as exc:
        raise RehearsalError("DB_PRECHECK_FAILED", "Selected rehearsal URL is invalid.") from exc
    if parsed.get_backend_name() != "postgresql" or parsed.database != expected_database:
        raise RehearsalError(
            "DB_PRECHECK_FAILED",
            f"Selected URL must name exactly {expected_database}; no connection was opened.",
        )
    if expected_database == REHEARSAL_DATABASE and not loopback_host(parsed.host or ""):
        raise RehearsalError("DB_PRECHECK_FAILED", "Rehearsal database host must be loopback-only.")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar_one()
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        engine.dispose()
        raise RehearsalError("DB_PRECHECK_FAILED", "Could not perform benchmark read-only preflight.") from exc
    if database != expected_database:
        engine.dispose()
        raise RehearsalError("DB_PRECHECK_FAILED", f"Expected {expected_database}, got {database!r}.")
    if revision != EXPECTED_ALEMBIC_REVISION:
        engine.dispose()
        raise RehearsalError("ALEMBIC_REVISION_FAILED", f"Expected {EXPECTED_ALEMBIC_REVISION}, got {revision!r}; no migration was run.")
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine, RehearsalState(database=database, revision=revision)


def verify_local_redis(environment: Mapping[str, str]) -> None:
    url = environment["REDIS_URL"]
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or not loopback_host(parsed.hostname):
        raise RehearsalError("REDIS_UNAVAILABLE", "Rehearsal accepts only a local loopback Redis URL.")
    if shutil.which("redis-cli") is None:
        raise RehearsalError("REDIS_UNAVAILABLE", "redis-cli is required for a local Redis readiness check.")
    result = subprocess.run(["redis-cli", "-u", url, "ping"], capture_output=True, text=True, check=False)
    if result.returncode or result.stdout.strip() != "PONG":
        raise RehearsalError("REDIS_UNAVAILABLE", "Local Redis did not answer PING.")


def verify_factory_not_running() -> None:
    if shutil.which("tmux") is None:
        return
    result = subprocess.run(["tmux", "has-session", "-t", "factory"], capture_output=True, check=False)
    if result.returncode == 0:
        raise RehearsalError("FACTORY_STACK_RUNNING", "tmux session 'factory' is running; stop it before rehearsal.")


def assert_ports_available() -> None:
    targets = (
        (socket.SOCK_STREAM, API_PORT),
        (socket.SOCK_DGRAM, VISION_STATUS_PORT),
        (socket.SOCK_DGRAM, VISION_REQUEST_PORT),
        (socket.SOCK_DGRAM, FMS_RESULT_PORT),
    )
    for kind, port in targets:
        probe = socket.socket(socket.AF_INET, kind)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            label = "TCP" if kind == socket.SOCK_STREAM else "UDP"
            raise RehearsalError("PORT_CONFLICT", f"{label} 127.0.0.1:{port} is already in use; nothing was stopped.") from exc
        finally:
            probe.close()


def verify_job_is_safe(session_factory: sessionmaker[Session], job_id: int, *, resume_unsent: bool = False) -> tuple[int, str]:
    """FMS scans globally; block rather than let it touch another benchmark job."""
    active = {
        JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
        JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY,
    }
    with session_factory() as session:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise RehearsalError("JOB_PREPARATION_FAILED", f"Job {job_id} does not exist in benchmark.")
        if job.status is not JobStatus.PRE_ROOF_READY:
            raise RehearsalError("JOB_PREPARATION_FAILED", f"Job {job_id} is {job.status.value}, not PRE_ROOF_READY; no state was changed.")
        others = list(session.scalars(select(ProductionJob).where(ProductionJob.status.in_(active), ProductionJob.job_id != job_id)))
        if others:
            labels = ", ".join(f"{item.job_id}:{item.job_code}:{item.status.value}" for item in others[:5])
            raise RehearsalError("OTHER_ACTIVE_JOBS", f"FMS scans globally; refusing while other active benchmark jobs exist: {labels}")
        latest = session.scalar(
            select(ProductionInspection)
            .where(ProductionInspection.production_job_id == job_id, ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF)
            .order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc())
            .limit(1)
        )
        if resume_unsent:
            if latest is None or latest.status is not ProductionInspectionStatus.RUNNING:
                raise RehearsalError("RESUME_UNSAFE", "--resume-unsent requires the latest RUNNING PRE_ROOF inspection.")
            evidence = (
                latest.wire_request_snapshot_json, latest.wire_request_digest,
                latest.wire_sent_at, latest.wire_acked_at, latest.wire_result_digest,
                latest.wire_error_code, latest.result, latest.completed_at,
            )
            if any(value is not None for value in evidence) or latest.wire_retry_count != 0 or latest.production_valid or latest.vision_production_valid or latest.results:
                raise RehearsalError("RESUME_UNSAFE", "Only a RUNNING inspection without wire/result evidence can be resumed by this rehearsal.")
        elif latest is not None and latest.status is ProductionInspectionStatus.RUNNING:
            raise RehearsalError("JOB_PREPARATION_FAILED", "Target is RUNNING; use --resume-unsent only if it has no persisted wire/result evidence.")
        return job.job_id, job.job_code


def wait_until(predicate, *, timeout: float, phase: str, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RehearsalError(phase, detail)


def process_alive(process: subprocess.Popen[bytes]) -> bool:
    return process.poll() is None


def udp_port_bound(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def api_is_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def post_start(job_id: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:8000/production/jobs/{job_id}/pre-roof/start",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise RehearsalError("START_API_FAILED", f"Start API returned {response.status}.")
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RehearsalError("START_API_FAILED", f"Start API returned {exc.code}: {body}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise RehearsalError("START_API_FAILED", "Could not call the local PRE_ROOF start API.") from exc


def latest_projection(session_factory: sessionmaker[Session], job_id: int) -> dict[str, Any] | None:
    with session_factory() as session:
        rows = UnityProductionInspectionProjectionService(session).get_snapshots(job_ids=[job_id])
        return next((item for item in rows if item["inspection_type"] == ProductionInspectionType.PRE_ROOF.value), None)


def wait_for_final_projection(session_factory: sessionmaker[Session], job_id: int, *, timeout: float) -> dict[str, Any]:
    output: dict[str, Any] | None = None

    def done() -> bool:
        nonlocal output
        output = latest_projection(session_factory, job_id)
        inspection = output.get("inspection") if output else None
        return bool(inspection and inspection["status"] in {"COMPLETED", "ERROR"})

    wait_until(done, timeout=timeout, phase="RESULT_NOT_APPLIED", detail="No terminal PRE_ROOF canonical state was persisted.")
    assert output is not None
    return output


def wait_for_ack_projection(session_factory, job_id: int, *, inspection_id: int, timeout: float):
    output = None

    def done():
        nonlocal output
        output = latest_projection(session_factory, job_id)
        inspection = output.get("inspection", {}) if output else {}
        if inspection.get("inspection_id") != inspection_id:
            raise RehearsalError("ACK_NOT_APPLIED", "Latest inspection changed while waiting for ACK metadata.")
        transport = inspection.get("transport", {})
        if inspection.get("status") == "ERROR" or transport.get("wire_error_code"):
            raise RehearsalError("ACK_NOT_APPLIED", f"Transport failed: {transport.get('wire_error_code')}")
        return transport.get("acked") is True and transport.get("acked_at") is not None

    wait_until(done, timeout=timeout, phase="ACK_NOT_APPLIED",
               detail="Final result persisted, but ACK metadata remained absent for the configured ACK/retry window.")
    return output


def verify_final_projection(projection: dict[str, Any], scenario: str) -> None:
    inspection = projection["inspection"]
    expected_result = {
        "all-pass-valid-false": "PASS",
        "fail": "FAIL",
        "not-evaluated-runtime": "NOT_EVALUATED",
        "not-evaluated-error": "NOT_EVALUATED",
    }[scenario]
    if inspection["status"] != "COMPLETED" or inspection["result"] != expected_result:
        raise RehearsalError("RESULT_NOT_APPLIED", f"Expected COMPLETED/{expected_result}, got {inspection['status']}/{inspection['result']}.")
    transport = inspection["transport"]
    if not transport["acked"] or transport["acked_at"] is None:
        raise RehearsalError("ACK_NOT_APPLIED", "Final result arrived but persisted ACK metadata is absent.")
    if transport["retry_count"] != 0:
        raise RehearsalError("ACK_NOT_APPLIED", f"Expected retry_count=0, got {transport['retry_count']}.")
    if inspection["vision_production_valid"] is not False or inspection["production_valid"] is not False:
        raise RehearsalError("UNEXPECTED_ROOF_RELEASE", "The selected rehearsal scenario must keep both validity values false.")
    if inspection["gate_state"] != "NOT_RELEASED":
        raise RehearsalError("UNEXPECTED_ROOF_RELEASE", "PASS + vision_production_valid=false must keep the Roof gate closed.")
    if scenario == "all-pass-valid-false":
        views = inspection["views"]
        if [view["view_name"] for view in views] != ["TOP", "LEFT", "RIGHT", "FRONT", "BEHIND"] or any(view["result"] != "PASS" for view in views):
            raise RehearsalError("RESULT_NOT_APPLIED", "Expected five persisted PASS view rows in contract order.")
        versions = inspection["runtime_versions"]
        if inspection["runtime_profile"] != "PRE_ROOF_5VIEW" or not isinstance(versions, dict) or versions.get("controller") != "V3":
            raise RehearsalError("RESULT_NOT_APPLIED", "Persisted runtime metadata is not PRE_ROOF_5VIEW/controller V3.")


async def observe_realtime_and_snapshot(job_id: int, *, timeout: float, resume_inspection_id: int | None = None, start_fms=None, ack_wait_seconds: float = 3.0, wait_for_ack=None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    import websockets

    uri = "ws://127.0.0.1:8000/ws/unity"
    async with websockets.connect(uri, open_timeout=5) as ws:
        initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if initial.get("type") != "production_snapshot":
            raise RehearsalError("UNITY_REALTIME_TIMEOUT", "First Unity envelope was not production_snapshot.")
        if resume_inspection_id is None:
            response = await asyncio.to_thread(post_start, job_id)
        else:
            rows = initial.get("data", {}).get("production_inspections", [])
            current = next((row.get("inspection", {}) for row in rows
                            if row.get("job_id") == job_id and row.get("inspection_type") == "PRE_ROOF"), {})
            if current.get("inspection_id") != resume_inspection_id or current.get("status") != "RUNNING":
                raise RehearsalError("RESUME_UNSAFE", "Initial snapshot no longer matches the preflight RUNNING inspection.")
            response = {"resumed": True, "inspection_id": resume_inspection_id,
                        "inspection_request_id": current["inspection_request_id"],
                        "inspection_cycle": current["inspection_cycle"]}
        # Subscribe before launching FMS, including resume of an already RUNNING
        # row, so a fast Fake result cannot precede the realtime observer.
        if start_fms is not None:
            await asyncio.to_thread(start_fms)
        latest: dict[str, Any] | None = None
        deadline = time.monotonic() + timeout
        final_seen = False
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(1.0, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                continue
            if message.get("type") != "production_inspection_status":
                continue
            data = message.get("data")
            inspection = data.get("inspection") if isinstance(data, dict) else None
            if data and data.get("job_id") == job_id and isinstance(inspection, dict):
                latest = data
                if inspection.get("status") == "ERROR":
                    raise RehearsalError("RESULT_NOT_APPLIED", f"PRE_ROOF ERROR: {inspection.get('transport', {}).get('wire_error_code')}")
                if inspection.get("status") == "COMPLETED":
                    if not final_seen:
                        final_seen = True
                        deadline = time.monotonic() + ack_wait_seconds
                        if wait_for_ack is not None:
                            await asyncio.to_thread(wait_for_ack, inspection["inspection_id"])
                    if inspection.get("transport", {}).get("acked") is True:
                        break
        if final_seen and latest["inspection"].get("transport", {}).get("acked") is not True:
            raise RehearsalError("ACK_NOT_APPLIED", "No ACK-bearing terminal projection arrived within the configured ACK/retry window.")
        if latest is None or latest["inspection"].get("status") != "COMPLETED":
            raise RehearsalError("UNITY_REALTIME_TIMEOUT", "No terminal production_inspection_status arrived on the real WebSocket.")
    async with websockets.connect(uri, open_timeout=5) as reconnect:
        snapshot_envelope = json.loads(await asyncio.wait_for(reconnect.recv(), timeout=5))
    if snapshot_envelope.get("type") != "production_snapshot":
        raise RehearsalError("SNAPSHOT_MISMATCH", "Reconnect did not begin with production_snapshot.")
    inspections = snapshot_envelope.get("data", {}).get("production_inspections", [])
    snapshot = next((item for item in inspections if item.get("job_id") == job_id and item.get("inspection_type") == "PRE_ROOF"), None)
    if snapshot is None:
        raise RehearsalError("SNAPSHOT_MISMATCH", "Reconnect snapshot omitted the target PRE_ROOF inspection.")
    return response, latest, snapshot


def realtime_timeout_diagnostic(session_factory, job_id, fms, log_path) -> RehearsalError:
    alive = fms is not None and process_alive(fms)
    try:
        projection = latest_projection(session_factory, job_id)
    except Exception:
        return RehearsalError("UNITY_REALTIME_TIMEOUT", f"FMS alive={alive}; DB diagnostic unavailable; FMS log={log_path}")
    inspection = projection["inspection"] if projection else {}
    transport = inspection.get("transport", {})
    phase = "UNITY_REALTIME_TIMEOUT"
    if not alive:
        phase = "FMS_PROCESS_EXITED"
    elif inspection.get("status") == "ERROR":
        phase = "RESULT_NOT_APPLIED"
    elif inspection.get("status") == "RUNNING":
        phase = ("REQUEST_NOT_SENT" if transport.get("request_sent_at") is None else
                 "ACK_NOT_APPLIED" if not transport.get("acked") else "RESULT_NOT_APPLIED")
    detail = (f"FMS alive={alive}; inspection_status={inspection.get('status')}; "
              f"wire_error_code={transport.get('wire_error_code')}; "
              f"wire_sent_at={transport.get('request_sent_at')}; wire_acked_at={transport.get('acked_at')}; "
              f"FMS log={log_path}")
    return RehearsalError(phase, detail)


def compare_realtime_and_snapshot(realtime: dict[str, Any], snapshot: dict[str, Any]) -> None:
    left, right = realtime["inspection"], snapshot["inspection"]
    fields = (
        "inspection_request_id", "inspection_cycle", "status", "result",
        "vision_production_valid", "production_valid", "gate_state", "views",
        "runtime_profile", "runtime_versions",
    )
    mismatches = [field for field in fields if left.get(field) != right.get(field)]
    if mismatches:
        raise RehearsalError("SNAPSHOT_MISMATCH", "Realtime/snapshot differ for: " + ", ".join(mismatches))


def report_success(state: RehearsalState) -> None:
    assert state.inspection is not None
    inspection = state.inspection["inspection"]
    print("\nPRE_ROOF FULL-STACK REHEARSAL\n")
    print(f"Database              {state.database}  OK")
    print(f"Alembic               {state.revision}  OK")
    print("API                    RUNNING                 OK")
    print("FMS                    RUNNING                 OK")
    print("Redis                  CONNECTED               OK")
    print("Fake Vision Request    127.0.0.1:20061         OK")
    print("FMS Result Listener    127.0.0.1:20062         OK")
    print(f"\nJob                    {state.job_id} ({state.job_code})")
    print(f"Cycle                  {inspection['inspection_cycle']}")
    print(f"Request ID             {inspection['inspection_request_id']}")
    print("\nRequest sent           YES")
    print(f"ACK received           {'YES' if inspection['transport']['acked'] else 'NO'}")
    print(f"Retry count            {inspection['transport']['retry_count']}")
    print(f"\nFinal status           {inspection['status']}")
    print(f"Result                 {inspection['result']}")
    print(f"Vision valid           {str(inspection['vision_production_valid']).lower()}")
    print(f"Server valid           {str(inspection['production_valid']).lower()}")
    print(f"Gate                   {inspection['gate_state']}")
    print(f"Views                  {len(inspection['views'])}/5 PASS")
    print(f"Runtime controller     {inspection['runtime_versions']['controller']}")
    print("\nUnity realtime         PASS")
    print("Reconnect snapshot     PASS")
    print("Roof materialized      NO")
    print("\nOVERALL:\nPASS")


def run(args: argparse.Namespace) -> int:
    if args.job_id < 1:
        raise RehearsalError("JOB_PREPARATION_FAILED", "--job-id must be a positive rehearsal job id.")
    if not PYTHON.exists():
        raise RehearsalError("ENVIRONMENT_FAILED", f"Missing venv Python: {PYTHON}")
    verify_source_bypasses_removed()
    parent = load_dotenv_environment()
    expected_database = {
        "benchmark": BENCHMARK_DATABASE,
        "rehearsal": REHEARSAL_DATABASE,
    }[args.database_target]
    child_env = safe_child_environment(parent, database_target=args.database_target)
    session_factory, engine, state = rehearsal_session_factory(
        child_env, expected_database=expected_database
    )
    try:
        verify_local_redis(child_env)
        verify_factory_not_running()
        assert_ports_available()
        state.job_id, state.job_code = verify_job_is_safe(session_factory, args.job_id, resume_unsent=args.resume_unsent)
        resume_id = (latest_projection(session_factory, args.job_id)["inspection"]["inspection_id"]
                     if args.resume_unsent else None)
        log_dir = ROOT / "logs" / "pre_roof_rehearsal" / time.strftime("%Y%m%dT%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=False)
        state.fake_log, state.api_log, state.fms_log = log_dir / "fake_vision.log", log_dir / "api.log", log_dir / "fms.log"
        owned = OwnedProcesses()
        try:
            fake = owned.start([
                str(PYTHON), "-m", "scripts.fake_vision_pre_roof_v01", "--host", "127.0.0.1",
                "--request-port", str(VISION_REQUEST_PORT), "--result-host", "127.0.0.1",
                "--result-port", str(FMS_RESULT_PORT), "--scenario", args.scenario,
            ], env=child_env, log_path=state.fake_log)
            wait_until(lambda: process_alive(fake) and udp_port_bound(VISION_REQUEST_PORT), timeout=5, phase="FAKE_VISION_BIND_FAILED", detail="Fake Vision did not bind 127.0.0.1:20061.")
            api = owned.start([str(PYTHON), "-m", "uvicorn", "api_server.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)], env=child_env, log_path=state.api_log)
            wait_until(lambda: process_alive(api) and api_is_healthy(), timeout=10, phase="API_START_FAILED", detail="API Server did not become healthy.")
            fms = None

            def start_fms():
                nonlocal fms
                fms = owned.start([str(PYTHON), "-m", "fms_server.main"], env=child_env, log_path=state.fms_log)
                wait_until(lambda: process_alive(fms) and udp_port_bound(FMS_RESULT_PORT), timeout=10, phase="FMS_RESULT_BIND_FAILED", detail="FMS did not bind the loopback PRE_ROOF result listener.")

            ack_wait_seconds = float(child_env["PRE_ROOF_UDP_ACK_TIMEOUT_SECONDS"]) * (
                int(child_env["PRE_ROOF_UDP_MAX_RETRIES"]) + 1
            )

            def wait_for_ack(inspection_id):
                return wait_for_ack_projection(session_factory, state.job_id,
                                               inspection_id=inspection_id, timeout=ack_wait_seconds)

            try:
                response, realtime, snapshot = asyncio.run(observe_realtime_and_snapshot(
                    state.job_id, timeout=args.timeout, resume_inspection_id=resume_id, start_fms=start_fms,
                    ack_wait_seconds=ack_wait_seconds, wait_for_ack=wait_for_ack,
                ))
            except RehearsalError as exc:
                if exc.phase == "UNITY_REALTIME_TIMEOUT":
                    raise realtime_timeout_diagnostic(session_factory, state.job_id, fms, state.fms_log) from exc
                raise
            state.request, state.realtime, state.snapshot = response, realtime, snapshot
            projection = wait_for_final_projection(session_factory, state.job_id, timeout=5)
            verify_final_projection(projection, args.scenario)
            compare_realtime_and_snapshot(realtime, snapshot)
            if projection != realtime:
                raise RehearsalError("SNAPSHOT_MISMATCH", "Realtime event differs from the current DB canonical projection.")
            state.inspection = projection
            report_success(state)
            print(f"\nLogs                   {log_dir}")
            return 0
        finally:
            owned.stop()
    finally:
        engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=int, required=True, help="Existing selected-database Job already in PRE_ROOF_READY.")
    parser.add_argument("--database-target", choices=("benchmark", "rehearsal"), default="benchmark", help="Explicit allowlisted database; benchmark remains the compatibility default.")
    parser.add_argument("--scenario", choices=SCENARIOS, default="all-pass-valid-false")
    parser.add_argument("--resume-unsent", action="store_true", help="Resume the same latest RUNNING inspection only if no wire/result evidence exists; skip POST start.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Realtime/final-result timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        print("INTERRUPTED: only rehearsal-owned processes were terminated.", file=sys.stderr)
        return 130
    except RehearsalError as exc:
        print(f"{exc.phase}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
