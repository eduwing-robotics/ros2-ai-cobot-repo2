"""Safe tmux launcher for local Factory development/runtime processes.

This script never runs migrations or performs data writes.  At start it makes
read-only ``current_database()`` and Alembic-revision checks before starting
any process.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SESSION = "factory"
RUNTIME = ROOT / "logs" / "runtime"
PLAN_FILE = RUNTIME / "factory_stack_plan.json"
BENCHMARK_DB = "smart_factory_benchmark"
PRODUCTION_DB = "smart_factory_db"
CELL_TRANSPORTS = ("fake", "ros2")
PREFETCH_MODES = ("disabled", "one_ahead")
VISION_KEYS = (
    "VISION_INCOMING_QA_UDP_HOST", "VISION_INCOMING_QA_UDP_PORT",
    "FMS_INCOMING_QA_RESULT_UDP_HOST", "FMS_INCOMING_QA_RESULT_UDP_PORT",
    "INCOMING_QA_UDP_ACK_TIMEOUT_SECONDS", "INCOMING_QA_UDP_MAX_RETRIES",
)
PRE_ROOF_VISION_KEYS = (
    "VISION_PRE_ROOF_UDP_HOST", "VISION_PRE_ROOF_UDP_PORT",
    "FMS_PRE_ROOF_RESULT_UDP_HOST", "FMS_PRE_ROOF_RESULT_UDP_PORT",
    "PRE_ROOF_UDP_ACK_TIMEOUT_SECONDS", "PRE_ROOF_UDP_MAX_RETRIES",
)
# Bind address is local-only and defaults in Settings; it must be removed with
# the other Vision configuration for a disabled profile, but is not required
# for an actual profile.
PRE_ROOF_VISION_ENV_KEYS = (*PRE_ROOF_VISION_KEYS, "FMS_PRE_ROOF_RESULT_UDP_BIND_HOST")


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class StackPlan:
    profile: str
    database_target: str
    cell_transport: str
    material_prefetch_mode: str
    telemetry_ros_enabled: bool
    voice_enabled: bool
    vision_mode: str
    api_enabled: bool = True
    fms_enabled: bool = True
    telemetry_enabled: bool = False

    def validate(self) -> None:
        if self.database_target not in {"benchmark", "production"}:
            raise LauncherError(f"Unsupported database target: {self.database_target}")
        if self.cell_transport not in CELL_TRANSPORTS:
            raise LauncherError("CELL_TRANSPORT must be one of: " + ", ".join(CELL_TRANSPORTS))
        if self.material_prefetch_mode not in PREFETCH_MODES:
            raise LauncherError("MATERIAL_PREFETCH_MODE must be one of: " + ", ".join(PREFETCH_MODES))
        if self.vision_mode not in {"disabled", "actual"}:
            raise LauncherError("Incoming Vision mode must be disabled or actual")
        if not any((self.api_enabled, self.fms_enabled, self.telemetry_enabled, self.voice_enabled)):
            raise LauncherError("Select at least one process to run")


def plan_for_profile(profile: str) -> StackPlan:
    """Actual is a device profile, not a production-DB profile."""
    if profile == "benchmark":
        return StackPlan(profile, "benchmark", "fake", "disabled", False, False, "disabled")
    if profile == "actual":
        # An actual hardware profile must never silently select a motion
        # transport. ``customize`` requires the operator to choose ros2 or
        # fake before this incomplete plan can be launched. The database
        # default intentionally remains benchmark: hardware profile and
        # production-DB opt-in are separate decisions.
        return StackPlan(profile, "benchmark", "", "disabled", True, True, "actual", telemetry_enabled=True)
    if profile == "custom":
        return StackPlan(profile, "benchmark", "fake", "disabled", False, False, "disabled")
    raise LauncherError("Profile must be benchmark, actual, or custom")


def validate_current_database(target: str, actual: str) -> None:
    expected = BENCHMARK_DB if target == "benchmark" else PRODUCTION_DB
    if actual != expected:
        raise LauncherError(f"Database validation failed: expected {expected}, got {actual or '<empty>'}")


def database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/").split("?", 1)[0] or "<unknown>"


def require_production_confirmation(value: str) -> None:
    if value != PRODUCTION_DB:
        raise LauncherError("Production DB not enabled. Type exactly smart_factory_db to opt in.")


def choose(
    prompt: str,
    values: tuple[str, ...],
    default: str,
    input_fn: Callable[[str], str] = input,
    *,
    require_explicit: bool = False,
) -> str:
    suffix = "[required]" if require_explicit else f"[{default}]"
    result = input_fn(f"{prompt} ({'/'.join(values)}) {suffix}: ").strip().lower()
    if not result and require_explicit:
        raise LauncherError(f"{prompt} requires an explicit selection: " + ", ".join(values))
    result = result or default
    if result not in values:
        raise LauncherError(f"Invalid selection {result!r}; choose one of: {', '.join(values)}")
    return result


def yes_no(prompt: str, default: bool, input_fn: Callable[[str], str] = input) -> bool:
    value = input_fn(f"{prompt} {'[Y/n]' if default else '[y/N]'} ").strip().lower()
    return default if not value else value in {"y", "yes"}


def customize(plan: StackPlan, input_fn: Callable[[str], str] = input) -> StackPlan:
    selected = StackPlan(
        plan.profile,
        choose("Database", ("benchmark", "production"), plan.database_target, input_fn),
        choose(
            "Robot Cell transport", CELL_TRANSPORTS, plan.cell_transport, input_fn,
            require_explicit=plan.profile == "actual",
        ),
        "disabled", yes_no("Enable Telemetry ROS", plan.telemetry_ros_enabled, input_fn),
        yes_no("Enable Voice Runtime", plan.voice_enabled, input_fn),
        choose("Incoming Vision QA", ("disabled", "actual"), plan.vision_mode, input_fn),
        yes_no("Run API Server", True, input_fn), yes_no("Run FMS Server", True, input_fn),
        yes_no("Run Telemetry Gateway", plan.telemetry_enabled, input_fn),
    )
    selected.validate()
    return selected


def dotenv_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Source existing .env exactly as shell entrypoints do; do not print it."""
    result = subprocess.run(
        ["bash", "-lc", "set -a; [ -f .env ] && source .env; set +a; env -0"],
        cwd=ROOT, env=dict(base or os.environ), capture_output=True, check=True,
    )
    env: dict[str, str] = {}
    for entry in result.stdout.split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            env[key.decode()] = value.decode(errors="surrogateescape")
    return env


def current_database(url: str, env: Mapping[str, str]) -> str:
    code = ("from sqlalchemy import create_engine,text;import os;"
            "e=create_engine(os.environ['DATABASE_URL']);"
            "print(e.connect().execute(text('SELECT current_database()')).scalar_one())")
    run_env = dict(env); run_env["DATABASE_URL"] = url
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", code], cwd=ROOT, env=run_env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise LauncherError("Could not verify current_database() for the selected database URL.")
    return result.stdout.strip()


def source_alembic_head() -> str:
    """Resolve the repository's single Alembic head without touching a DB."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise LauncherError(f"Expected exactly one Alembic source head, got: {', '.join(heads) or '<none>'}")
    return heads[0]


def current_alembic_revision(url: str, env: Mapping[str, str]) -> str:
    """Read the DB revision only; this launcher never invokes Alembic upgrade."""
    code = (
        "from sqlalchemy import create_engine,text;import os;"
        "e=create_engine(os.environ['DATABASE_URL']);"
        "print(e.connect().execute(text('SELECT version_num FROM alembic_version')).scalar_one())"
    )
    run_env = dict(env); run_env["DATABASE_URL"] = url
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", code], cwd=ROOT, env=run_env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise LauncherError("Could not read Alembic current revision for the selected database URL; no migration was run.")
    return result.stdout.strip()


def validate_alembic_revision(url: str, env: Mapping[str, str]) -> tuple[str, str]:
    expected = source_alembic_head()
    actual = current_alembic_revision(url, env)
    if actual != expected:
        raise LauncherError(
            "DATABASE_SCHEMA_REVISION_MISMATCH "
            f"expected={expected} actual={actual or '<empty>'}; no migration was run. "
            "Apply the required migration separately, then retry."
        )
    return expected, actual


def settings_from(env: Mapping[str, str]) -> dict[str, object]:
    code = """import json
from shared.config import get_settings
get_settings.cache_clear(); s=get_settings()
print(json.dumps({'api_host':s.api_host,'api_port':s.api_port,'telemetry_host':s.telemetry_host,'telemetry_port':s.telemetry_port,'redis_url':s.redis_url,'ros_domain_id':s.ros_domain_id,'fms_pre_roof_result_udp_host':s.fms_pre_roof_result_udp_host,'fms_pre_roof_result_udp_bind_host':s.fms_pre_roof_result_udp_bind_host,'fms_pre_roof_result_udp_port':s.fms_pre_roof_result_udp_port}))"""
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", code], cwd=ROOT, env=dict(env),
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise LauncherError("Could not load shared Settings for the selected environment.")
    return json.loads(result.stdout)


def validate_environment(plan: StackPlan, env: Mapping[str, str], input_fn: Callable[[str], str] = input) -> tuple[dict[str, str], dict[str, object], str]:
    plan.validate(); effective = dict(env)
    if plan.database_target == "benchmark":
        url = effective.get("POSTGRES_TEST_DATABASE_URL", "")
        if not url:
            raise LauncherError("POSTGRES_TEST_DATABASE_URL is required for benchmark/test launch.")
    else:
        url = effective.get("DATABASE_URL", "")
        if not url:
            raise LauncherError("DATABASE_URL is required for production opt-in.")
        print(f"Configured production database: {database_name(url)}")
        require_production_confirmation(input_fn(f"Type exactly {PRODUCTION_DB} to continue: ").strip())
    actual = current_database(url, effective)
    validate_current_database(plan.database_target, actual)
    # This is deliberately before all process/ROS/Redis startup. It is a
    # read-only SELECT and never substitutes for an explicit migration step.
    validate_alembic_revision(url, effective)
    effective.update(DATABASE_URL=url, CELL_TRANSPORT=plan.cell_transport,
                     MATERIAL_PREFETCH_MODE=plan.material_prefetch_mode,
                     TELEMETRY_ROS_ENABLED=str(plan.telemetry_ros_enabled).lower())
    if plan.vision_mode == "disabled":
        for key in (*VISION_KEYS, *PRE_ROOF_VISION_ENV_KEYS): effective.pop(key, None)
    else:
        missing = [key for key in (*VISION_KEYS, *PRE_ROOF_VISION_KEYS) if not effective.get(key)]
        if missing: raise LauncherError("Actual Vision profile requires: " + ", ".join(missing))
    return effective, settings_from(effective), actual


def redis_ready(url: str, env: Mapping[str, str]) -> bool:
    binary = shutil.which("redis-cli")
    if not binary: return False
    result = subprocess.run([binary, "-u", url, "ping"], env=dict(env), capture_output=True, text=True, check=False, timeout=3)
    return result.returncode == 0 and result.stdout.strip() == "PONG"


def port_owner(port: int) -> str | None:
    binary = shutil.which("ss")
    if not binary: return None
    result = subprocess.run([binary, "-ltnp", f"sport = :{port}"], capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return "\n".join(lines[1:]) if len(lines) > 1 else None


def udp_port_owner(port: int) -> str | None:
    """Return UDP listener evidence without stopping or altering it."""
    binary = shutil.which("ss")
    if not binary: return None
    result = subprocess.run([binary, "-lunp", f"sport = :{port}"], capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return "\n".join(lines[1:]) if len(lines) > 1 else None


def require_ros() -> None:
    paths = (Path("/opt/ros/jazzy/setup.bash"), Path.home() / "factory_ros_ws/install/setup.bash")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing: raise LauncherError("ROS setup missing: " + ", ".join(missing))


def shell_prefix(plan: StackPlan, settings: Mapping[str, object], needs_ros: bool) -> str:
    """Per-tmux-window init. Credentials stay in .env, outside tmux argv/logs."""
    db = 'export DATABASE_URL="$POSTGRES_TEST_DATABASE_URL";' if plan.database_target == "benchmark" else ""
    unset_vision = " ".join(
        f"unset {key};" for key in (*VISION_KEYS, *PRE_ROOF_VISION_ENV_KEYS)
    ) if plan.vision_mode == "disabled" else ""
    ros = 'source /opt/ros/jazzy/setup.bash; source "$HOME/factory_ros_ws/install/setup.bash";' if needs_ros else ""
    return (f"cd {shlex.quote(str(ROOT))}; set -a; [ -f .env ] && source .env; set +a; source .venv/bin/activate; {ros} {db} "
            f"export CELL_TRANSPORT={plan.cell_transport}; export MATERIAL_PREFETCH_MODE={plan.material_prefetch_mode}; "
            f"export TELEMETRY_ROS_ENABLED={'true' if plan.telemetry_ros_enabled else 'false'}; "
            f"export ROS_DOMAIN_ID={int(settings['ros_domain_id'])}; {unset_vision}")


def component_command(component: str, plan: StackPlan, settings: Mapping[str, object]) -> str:
    if component == "api":
        prefix, entry = shell_prefix(plan, settings, False), f"exec python -m uvicorn api_server.main:app --host {shlex.quote(str(settings['api_host']))} --port {int(settings['api_port'])}"
    elif component == "fms":
        prefix, entry = shell_prefix(plan, settings, plan.cell_transport == "ros2"), "exec python -m fms_server.main"
    elif component == "telemetry":
        prefix, entry = shell_prefix(plan, settings, plan.telemetry_ros_enabled), f"exec python -m uvicorn telemetry_gateway.main:app --host {shlex.quote(str(settings['telemetry_host']))} --port {int(settings['telemetry_port'])}"
    elif component == "voice":
        prefix, entry = shell_prefix(plan, settings, False), "exec python -m voice_runtime.main"
    else: raise LauncherError(f"Unknown component: {component}")
    return f"bash -lc {shlex.quote(prefix + ' ' + entry)}"


def display(plan: StackPlan, settings: Mapping[str, object], database: str) -> str:
    return "\n".join(("=" * 48, " Factory Stack", "=" * 48,
        f"Profile                {plan.profile}", f"Database               {database}",
        f"Robot Cell Transport   {plan.cell_transport}", f"Incoming Vision        {plan.vision_mode}",
        f"PRE_ROOF Vision        {'configured' if plan.vision_mode == 'actual' else 'disabled'}",
        f"PRE_ROOF Result advertised  {settings.get('fms_pre_roof_result_udp_host')}:{settings.get('fms_pre_roof_result_udp_port')}" if plan.vision_mode == 'actual' else "PRE_ROOF Result advertised  disabled",
        f"PRE_ROOF Result bind        {settings.get('fms_pre_roof_result_udp_bind_host')}:{settings.get('fms_pre_roof_result_udp_port')}" if plan.vision_mode == 'actual' else "PRE_ROOF Result bind        disabled",
        f"Telemetry ROS          {'enabled' if plan.telemetry_ros_enabled else 'disabled'}",
        f"ROS Domain             {settings['ros_domain_id']}", f"Voice                  {'enabled' if plan.voice_enabled else 'disabled'}",
        f"API                    :{settings['api_port'] if plan.api_enabled else 'disabled'}",
        f"Telemetry Gateway      :{settings['telemetry_port'] if plan.telemetry_enabled else 'disabled'}", "=" * 48))


def tmux_exists() -> bool:
    return subprocess.run(["tmux", "has-session", "-t", SESSION], capture_output=True, check=False).returncode == 0


def tmux(*args: str) -> None:
    subprocess.run(["tmux", *args], check=True)


def save_plan(plan: StackPlan, settings: Mapping[str, object], database: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PLAN_FILE.write_text(json.dumps({"plan":asdict(plan), "settings":settings, "database":database}, indent=2) + "\n")


def saved_plan() -> dict[str, object] | None:
    try: return json.loads(PLAN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError): return None


def start(profile: str) -> int:
    if not shutil.which("tmux"): raise LauncherError("tmux is required. Install it before starting the factory stack.")
    if tmux_exists(): raise LauncherError("Factory stack is already running. Use: factory_stack.sh attach | status | stop")
    plan = plan_for_profile(profile)
    if profile in {"actual", "custom"}: plan = customize(plan)
    effective, settings, database = validate_environment(plan, dotenv_environment())
    if plan.telemetry_ros_enabled or plan.cell_transport == "ros2": require_ros()
    redis = redis_ready(str(settings["redis_url"]), effective)
    if profile == "actual" and not redis: raise LauncherError("Redis is unreachable; actual profile refuses to start without realtime infrastructure.")
    if not shutil.which("ss"):
        raise LauncherError("The ss utility is required for fail-closed port conflict checks.")
    for name, enabled, port in (("API", plan.api_enabled, settings["api_port"]), ("Telemetry", plan.telemetry_enabled, settings["telemetry_port"])):
        if enabled and (owner := port_owner(int(port))): raise LauncherError(f"{name} port {port} is already in use:\n{owner}\nLauncher will not kill it.")
    if plan.fms_enabled and plan.vision_mode == "actual":
        for label, key in (("Incoming QA", "FMS_INCOMING_QA_RESULT_UDP_PORT"), ("PRE_ROOF", "FMS_PRE_ROOF_RESULT_UDP_PORT")):
            result_port = int(effective[key])
            if owner := udp_port_owner(result_port):
                raise LauncherError(f"FMS {label} result UDP port {result_port} is already in use:\n{owner}\nLauncher will not kill it.")
    print(display(plan, settings, database)); print(f"Redis                  {'CONNECTED' if redis else 'UNREACHABLE (benchmark may run without realtime)'}")
    save_plan(plan, settings, database)
    components = [name for name, on in (("api",plan.api_enabled),("fms",plan.fms_enabled),("telemetry",plan.telemetry_enabled),("voice",plan.voice_enabled)) if on]
    try:
        first, *rest = components
        tmux("new-session", "-d", "-s", SESSION, "-n", first, component_command(first, plan, settings))
        for component in rest: tmux("new-window", "-t", SESSION, "-n", component, component_command(component, plan, settings))
        wrapper = ROOT / "scripts/factory_stack.sh"
        tmux("new-window", "-t", SESSION, "-n", "diagnostics", f"bash -lc {shlex.quote(f'while true; do clear; {wrapper} status; sleep 2; done')}")
    except Exception:
        if tmux_exists(): subprocess.run(["tmux", "kill-session", "-t", SESSION], check=False)
        raise
    print("Factory stack started. Use ./scripts/factory_stack.sh attach or status.")
    return 0


def health(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as response: payload = json.loads(response.read().decode())
        return "OK " + ", ".join(f"{key}={value}" for key, value in payload.items())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError): return "UNREACHABLE"


def status() -> int:
    if not shutil.which("tmux") or not tmux_exists(): print("Factory stack: STOPPED"); return 1
    saved = saved_plan()
    if not saved: print("Factory stack: RUNNING (launch metadata unavailable)"); return 0
    plan, settings = StackPlan(**saved["plan"]), saved["settings"]
    print(display(plan, settings, str(saved["database"])))
    windows = subprocess.run(["tmux","list-windows","-t",SESSION,"-F","#{window_name}:#{pane_dead}"], capture_output=True,text=True,check=False).stdout.splitlines()
    state = dict(item.split(":", 1) for item in windows if ":" in item)
    for component, enabled in (("api",plan.api_enabled),("fms",plan.fms_enabled),("telemetry",plan.telemetry_enabled),("voice",plan.voice_enabled)):
        print(f"{component.upper():<12} {'NOT SELECTED' if not enabled else ('RUNNING' if state.get(component) == '0' else 'STOPPED')}")
    print(f"Redis        {'CONNECTED' if redis_ready(str(settings['redis_url']), dotenv_environment()) else 'UNREACHABLE'}")
    if plan.api_enabled: print(f"API health   {health(int(settings['api_port']))}")
    if plan.telemetry_enabled: print(f"Telemetry health {health(int(settings['telemetry_port']))}")
    return 0


def attach() -> int:
    if not shutil.which("tmux") or not tmux_exists(): raise LauncherError("Factory stack is not running.")
    os.execvp("tmux", ["tmux", "attach", "-t", SESSION]); return 0


def stop() -> int:
    if not shutil.which("tmux") or not tmux_exists(): print("Factory stack: already stopped"); return 0
    windows = subprocess.run(["tmux","list-windows","-t",SESSION,"-F","#{window_name}"],capture_output=True,text=True,check=False).stdout.splitlines()
    for window in windows:
        if window != "diagnostics": subprocess.run(["tmux","send-keys","-t",f"{SESSION}:{window}","C-c"],check=False)
    time.sleep(1); subprocess.run(["tmux","kill-session","-t",SESSION],check=False); print("Factory stack stopped."); return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start","status","attach","stop"))
    parser.add_argument("profile", nargs="?", choices=("benchmark","actual","custom"), default="benchmark")
    args = parser.parse_args(argv)
    try:
        return start(args.profile) if args.command == "start" else status() if args.command == "status" else attach() if args.command == "attach" else stop()
    except LauncherError as exc: print(f"[FAIL] {exc}", file=sys.stderr); return 2
    except subprocess.CalledProcessError as exc: print(f"[FAIL] Command failed safely: {exc.cmd}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
