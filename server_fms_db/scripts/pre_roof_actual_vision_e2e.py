#!/usr/bin/env python3
"""Run a guarded, operator-driven PRE_ROOF Actual Vision E2E harness.

The harness opens only API and FMS child processes against the local disposable
``smart_factory_rehearsal`` database. It never creates a Job, calls PRE_ROOF
start, sends a Fake Vision packet, or starts ROS/hardware processes. The
operator uses the existing Monitoring GUI to start the inspection.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from api_server.services.unity_production_inspection_projection_service import (
    UnityProductionInspectionProjectionService,
)
from scripts.pre_roof_full_stack_rehearsal import (
    API_PORT,
    EXPECTED_ALEMBIC_REVISION,
    FMS_RESULT_PORT,
    PYTHON,
    RehearsalError,
    OwnedProcesses,
    load_dotenv_environment,
    process_alive,
    rehearsal_session_factory,
    verify_factory_not_running,
    verify_local_redis,
    verify_source_bypasses_removed,
    wait_until,
)
from shared.models.factory import (
    AssemblyRecipeStage,
    JobStatus,
    JobStep,
    ProductionInspection,
    ProductionInspectionStatus,
    ProductionInspectionType,
    ProductionJob,
)
from scripts.rehearsal_database import (
    RehearsalDatabaseSafetyError,
    rehearsal_database_url,
)

REHEARSAL_DATABASE = "smart_factory_rehearsal"
ACTUAL_VISION_HOST = os.getenv("VISION_PRE_ROOF_UDP_HOST", "192.168.20.30")
ACTUAL_VISION_PORT = int(os.getenv("VISION_PRE_ROOF_UDP_PORT", "20061"))
ACTUAL_RESULT_HOST = os.getenv("FMS_PRE_ROOF_RESULT_UDP_HOST", "192.168.20.20")
ACTUAL_RESULT_PORT = int(os.getenv("FMS_PRE_ROOF_RESULT_UDP_PORT", "20062"))
ACTUAL_MONITOR_HOST = os.getenv("MAIN_SERVER_IP", ACTUAL_RESULT_HOST)
LOCAL_VISION_STATUS_PORT = 20050
ACTIVE_JOB_STATUSES = frozenset({
    JobStatus.REQUESTED, JobStatus.READY, JobStatus.RUNNING,
    JobStatus.PRE_ROOF_READY, JobStatus.ROOF_READY,
})


class ActualVisionHarnessError(RehearsalError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", type=int, required=True,
                        help="Existing smart_factory_rehearsal Job in PRE_ROOF_READY.")
    parser.add_argument("--status-interval", type=float, default=5.0,
                        help="Read-only diagnostic interval in seconds; must be positive.")
    return parser.parse_args(argv)


def actual_child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """Build the only child environment permitted for Actual Vision E2E."""
    try:
        rehearsal_url = rehearsal_database_url(environment=parent)
    except RehearsalDatabaseSafetyError as exc:
        raise ActualVisionHarnessError("DB_PRECHECK_FAILED", str(exc)) from exc
    env = dict(parent)
    env.update({
        "DATABASE_URL": rehearsal_url,
        "CELL_TRANSPORT": "fake",
        "MATERIAL_PREFETCH_MODE": "disabled",
        "TELEMETRY_ROS_ENABLED": "false",
        "API_HOST": "0.0.0.0",
        "API_PORT": str(API_PORT),
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        # The FMS currently owns this generic status receiver. Keep it local;
        # PRE_ROOF E2E has no Vision-status UDP dependency.
        "VISION_STATUS_UDP_HOST": "127.0.0.1",
        "VISION_STATUS_UDP_PORT": str(LOCAL_VISION_STATUS_PORT),
        "VISION_PRE_ROOF_UDP_HOST": ACTUAL_VISION_HOST,
        "VISION_PRE_ROOF_UDP_PORT": str(ACTUAL_VISION_PORT),
        "FMS_PRE_ROOF_RESULT_UDP_HOST": ACTUAL_RESULT_HOST,
        "FMS_PRE_ROOF_RESULT_UDP_BIND_HOST": "0.0.0.0",
        "FMS_PRE_ROOF_RESULT_UDP_PORT": str(ACTUAL_RESULT_PORT),
        "PRE_ROOF_UDP_ACK_TIMEOUT_SECONDS": "1",
        "PRE_ROOF_UDP_MAX_RETRIES": "2",
    })
    # No Incoming QA dispatch exists in this harness. Removing all settings
    # prevents the FMS reconciliation runtime from sending actual QA requests.
    for key in (
        "VISION_INCOMING_QA_UDP_HOST", "VISION_INCOMING_QA_UDP_PORT",
        "FMS_INCOMING_QA_RESULT_UDP_HOST", "FMS_INCOMING_QA_RESULT_UDP_PORT",
        "INCOMING_QA_UDP_ACK_TIMEOUT_SECONDS", "INCOMING_QA_UDP_MAX_RETRIES",
        "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "CYCLONEDDS_URI",
    ):
        env.pop(key, None)
    return env


def actual_child_commands() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The harness owns exactly API and FMS; no simulator/hardware command."""
    return (
        (str(PYTHON), "-m", "uvicorn", "api_server.main:app", "--host", "0.0.0.0", "--port", str(API_PORT)),
        (str(PYTHON), "-m", "fms_server.main"),
    )


def _assert_port_free(kind: int, host: str, port: int) -> None:
    probe = socket.socket(socket.AF_INET, kind)
    try:
        probe.bind((host, port))
    except OSError as exc:
        label = "TCP" if kind == socket.SOCK_STREAM else "UDP"
        raise ActualVisionHarnessError(
            "PORT_CONFLICT", f"{label} {host}:{port} is already in use; nothing was stopped."
        ) from exc
    finally:
        probe.close()


def assert_actual_ports_available() -> None:
    _assert_port_free(socket.SOCK_STREAM, "0.0.0.0", API_PORT)
    _assert_port_free(socket.SOCK_DGRAM, "0.0.0.0", ACTUAL_RESULT_PORT)
    _assert_port_free(socket.SOCK_DGRAM, "127.0.0.1", LOCAL_VISION_STATUS_PORT)


def _latest_inspection(session: Session, job_id: int) -> ProductionInspection | None:
    return session.scalar(select(ProductionInspection).where(
        ProductionInspection.production_job_id == job_id,
        ProductionInspection.inspection_type == ProductionInspectionType.PRE_ROOF,
    ).order_by(ProductionInspection.inspection_cycle.desc(), ProductionInspection.inspection_id.desc()).limit(1))


def verify_actual_vision_job(factory: sessionmaker[Session], *, job_id: int) -> tuple[int, str, str]:
    """Read-only precheck for the sole Job the global FMS may observe."""
    with factory() as session:
        job = session.get(ProductionJob, job_id)
        if job is None:
            raise ActualVisionHarnessError("JOB_PRECHECK_FAILED", f"Job {job_id} does not exist in {REHEARSAL_DATABASE}.")
        if job.status is not JobStatus.PRE_ROOF_READY:
            raise ActualVisionHarnessError("JOB_PRECHECK_FAILED", f"Job {job_id} is {job.status.value}, not PRE_ROOF_READY.")
        others = list(session.scalars(select(ProductionJob).where(
            ProductionJob.status.in_(ACTIVE_JOB_STATUSES), ProductionJob.job_id != job_id,
        )))
        if others:
            labels = ", ".join(f"{row.job_id}:{row.job_code}:{row.status.value}" for row in others[:5])
            raise ActualVisionHarnessError("EXTRA_ACTIVE_JOB", "FMS global scan blocked by: " + labels)
        roof_count = session.scalar(select(func.count()).select_from(JobStep).join(
            AssemblyRecipeStage,
            JobStep.source_recipe_stage_id == AssemblyRecipeStage.recipe_stage_id,
        ).where(
            JobStep.job_id == job_id,
            AssemblyRecipeStage.execution_gate == "PRE_ROOF_PASS",
        )) or 0
        if roof_count:
            raise ActualVisionHarnessError("ROOF_ALREADY_MATERIALIZED", "A PRE_ROOF-gated JobStep already exists.")
        latest = _latest_inspection(session, job_id)
        if latest is None:
            raise ActualVisionHarnessError("JOB_PRECHECK_FAILED", "PRE_ROOF PENDING placeholder is missing.")
        if latest.status is ProductionInspectionStatus.PENDING:
            mode = "INITIAL_START"
        elif latest.status is ProductionInspectionStatus.COMPLETED and latest.production_valid is False:
            mode = "REINSPECTION"
        elif latest.status is ProductionInspectionStatus.RUNNING:
            raise ActualVisionHarnessError("JOB_RUNNING", "Latest PRE_ROOF inspection is RUNNING; do not start another harness.")
        else:
            raise ActualVisionHarnessError(
                "JOB_PRECHECK_FAILED",
                f"Latest PRE_ROOF cycle is {latest.status.value}/production_valid={latest.production_valid}; GUI start is not allowed.",
            )
        return job.job_id, job.job_code, mode


def latest_status(factory: sessionmaker[Session], job_id: int) -> dict[str, Any] | None:
    with factory() as session:
        rows = UnityProductionInspectionProjectionService(session).get_snapshots(job_ids=[job_id])
        return next((row for row in rows if row["inspection_type"] == "PRE_ROOF"), None)


def api_is_healthy() -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def udp_port_bound(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def _print_status(factory: sessionmaker[Session], job_id: int) -> None:
    snapshot = latest_status(factory, job_id)
    inspection = snapshot.get("inspection") if snapshot else None
    if not inspection:
        print("Inspection              <none>")
        return
    transport = inspection.get("transport", {})
    print(
        "Inspection              "
        f"status={inspection.get('status')} result={inspection.get('result')} "
        f"vision_valid={inspection.get('vision_production_valid')} "
        f"production_valid={inspection.get('production_valid')} gate={inspection.get('gate_state')}\n"
        "Transport               "
        f"sent={transport.get('request_sent_at') is not None} "
        f"acked={transport.get('acked')} retries={transport.get('retry_count')} "
        f"wire_error_code={transport.get('wire_error_code')}"
    )


def report_ready(*, database: str, revision: str, job_id: int, job_code: str, mode: str, log_dir: Path) -> None:
    print("\nPRE_ROOF ACTUAL VISION E2E\n")
    print(f"Database              {database} OK")
    print(f"Alembic               {revision} OK")
    print("API                    RUNNING OK")
    print("FMS                    RUNNING OK")
    print("Redis                  CONNECTED OK")
    print(f"Job                    {job_id} ({job_code}) {mode}")
    print(f"Vision Request Target  {ACTUAL_VISION_HOST}:{ACTUAL_VISION_PORT}")
    print(f"FMS Result Listener    0.0.0.0:{ACTUAL_RESULT_PORT}")
    print(f"Advertised Result      {ACTUAL_RESULT_HOST}:{ACTUAL_RESULT_PORT}")
    print("Robot/ROS              DISABLED")
    print("Fake Vision            DISABLED")
    print("Monitoring GUI         http://127.0.0.1:8000/production/monitor")
    print(f"LAN Monitoring GUI     http://{ACTUAL_MONITOR_HOST}:8000/production/monitor")
    print(f"Logs                   {log_dir}")
    print("\nREADY FOR OPERATOR\n")


def run(args: argparse.Namespace) -> int:
    if args.job_id < 1 or args.status_interval <= 0:
        raise ActualVisionHarnessError("ARGUMENT_INVALID", "--job-id and --status-interval must be positive.")
    if not PYTHON.exists():
        raise ActualVisionHarnessError("ENVIRONMENT_FAILED", f"Missing venv Python: {PYTHON}")
    verify_source_bypasses_removed()
    parent = load_dotenv_environment()
    child_env = actual_child_environment(parent)
    factory, engine, state = rehearsal_session_factory(child_env, expected_database=REHEARSAL_DATABASE)
    try:
        verify_local_redis(child_env)
        verify_factory_not_running()
        assert_actual_ports_available()
        job_id, job_code, mode = verify_actual_vision_job(factory, job_id=args.job_id)
        log_dir = ROOT / "logs" / "pre_roof_actual_vision_e2e" / time.strftime("%Y%m%dT%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=False)
        api_command, fms_command = actual_child_commands()
        owned = OwnedProcesses()
        try:
            api = owned.start(list(api_command), env=child_env, log_path=log_dir / "api.log")
            wait_until(lambda: process_alive(api) and api_is_healthy(), timeout=10,
                       phase="API_START_FAILED", detail="API did not become healthy.")
            fms = owned.start(list(fms_command), env=child_env, log_path=log_dir / "fms.log")
            wait_until(lambda: process_alive(fms) and udp_port_bound(ACTUAL_RESULT_PORT), timeout=10,
                       phase="FMS_RESULT_BIND_FAILED", detail="FMS did not bind 0.0.0.0:20062.")
            report_ready(database=state.database, revision=state.revision, job_id=job_id,
                         job_code=job_code, mode=mode, log_dir=log_dir)
            while True:
                _print_status(factory, job_id)
                time.sleep(args.status_interval)
        finally:
            owned.stop()
            print("ACTUAL_VISION_E2E_STOPPED: only harness-owned API/FMS processes were terminated; DB, Redis, and Vision were untouched.")
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        print("ACTUAL_VISION_E2E_INTERRUPTED: only harness-owned API/FMS processes were terminated.", file=sys.stderr)
        return 130
    except RehearsalError as exc:
        print(f"{exc.phase}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
