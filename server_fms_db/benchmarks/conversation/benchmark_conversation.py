"""Formal, HTTP end-to-end benchmark for the text production conversation flow.

This runner intentionally does not import or call the conversation service.  It sends
real requests to ``POST /ai/conversation`` and uses a benchmark-only PostgreSQL
database strictly for read-only post-condition checks.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DATASET = Path(__file__).resolve().with_name("conversation_dataset_v2.json")
DEFAULT_RESULTS_DIR = Path(__file__).resolve().with_name("results")
DEFAULT_BENCHMARK_DB = "smart_factory_benchmark"
CORE_FILES = (
    "api_server/services/production_conversation_service.py",
    "api_server/services/command_interpreter.py",
    "api_server/services/llm_service.py",
    "api_server/services/response_message_builder.py",
    "shared/services/pending_production_request_service.py",
    "shared/services/production_request_materialization_service.py",
    "shared/services/production_orchestration_service.py",
)


@dataclass(frozen=True)
class Dataset:
    version: str
    scenarios: list[dict[str, Any]]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run_command(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(args, cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def load_dataset(path: Path) -> Dataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    version, scenarios = raw.get("dataset_version"), raw.get("scenarios")
    if not isinstance(version, str) or not version:
        raise ValueError("dataset_version이 필요합니다.")
    if not isinstance(scenarios, list) or len(scenarios) != 30:
        raise ValueError("Conversation Dataset v2는 정확히 30개 scenario여야 합니다.")
    expected_categories = {
        "A_direct_roof_confirm": 8,
        "B_roof_followup_confirm": 8,
        "C_rejection": 4,
        "D_invalid_roof_recovery": 3,
        "E_invalid_confirmation_recovery": 2,
        "F_no_pending_safety": 3,
        "G_duplicate_confirmation_safety": 2,
    }
    category_counts = {category: 0 for category in expected_categories}
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ValueError("scenario는 객체여야 합니다.")
        scenario_id, category, turns, expected = (
            scenario.get("scenario_id"), scenario.get("category"), scenario.get("turns"), scenario.get("expected")
        )
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenario_ids:
            raise ValueError(f"유효하지 않거나 중복된 scenario_id: {scenario_id!r}")
        scenario_ids.add(scenario_id)
        if category not in category_counts:
            raise ValueError(f"{scenario_id}: 알 수 없는 category")
        category_counts[category] += 1
        if not isinstance(turns, list) or not turns or not isinstance(expected, dict):
            raise ValueError(f"{scenario_id}: turns 및 expected가 필요합니다.")
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("text"), str) or not turn["text"].strip():
                raise ValueError(f"{scenario_id}: 각 turn의 text가 필요합니다.")
            if "expect_state" not in turn:
                raise ValueError(f"{scenario_id}: 각 turn에는 expect_state가 필요합니다.")
    if category_counts != expected_categories:
        raise ValueError(f"Dataset category count mismatch: {category_counts}")
    return Dataset(version=version, scenarios=scenarios)


def assert_benchmark_database(database_url: str, expected_database: str) -> URL:
    parsed = make_url(database_url)
    if parsed.database != expected_database:
        raise ValueError(
            f"Benchmark DB safety guard: expected database {expected_database!r}, got {parsed.database!r}."
        )
    return parsed


def database_snapshot(engine: Any, pending_request_id: int | None) -> dict[str, Any]:
    if pending_request_id is None:
        return {"final_state": None, "jobs": [], "step_count": 0, "event_count": 0}
    with engine.connect() as connection:
        state = connection.execute(
            text("SELECT state FROM pending_production_requests WHERE request_id = :request_id"),
            {"request_id": pending_request_id},
        ).scalar_one_or_none()
        jobs = list(
            connection.execute(
                text(
                    """
                    SELECT j.job_id, j.job_code, p.product_code, j.roof_option_code::text AS roof_option_code,
                           j.source_item_index
                    FROM production_jobs AS j
                    JOIN products AS p ON p.product_id = j.product_id
                    WHERE j.source_pending_request_id = :request_id
                    ORDER BY j.source_item_index
                    """
                ),
                {"request_id": pending_request_id},
            ).mappings()
        )
        step_count = connection.execute(
            text(
                "SELECT count(*) FROM job_steps AS js "
                "JOIN production_jobs AS j ON j.job_id = js.job_id "
                "WHERE j.source_pending_request_id = :request_id"
            ),
            {"request_id": pending_request_id},
        ).scalar_one()
        event_count = connection.execute(
            text(
                "SELECT count(*) FROM production_events AS e "
                "JOIN production_jobs AS j ON j.job_id = e.job_id "
                "WHERE j.source_pending_request_id = :request_id AND e.event_type = 'JOB_CREATED'"
            ),
            {"request_id": pending_request_id},
        ).scalar_one()
    return {"final_state": state, "jobs": [dict(job) for job in jobs], "step_count": step_count, "event_count": event_count}


def post_conversation(client: httpx.Client, base_url: str, session_id: str, input_text: str) -> tuple[int, Any, float, str]:
    started = time.perf_counter()
    response: httpx.Response | None = None
    try:
        response = client.post(f"{base_url.rstrip('/')}/ai/conversation", json={"session_id": session_id, "text": input_text})
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        error = "" if response.is_success else str((body or {}).get("detail", response.text))[:500]
        return response.status_code, body, latency_ms, error
    except httpx.HTTPError as error:
        return 0, None, (time.perf_counter() - started) * 1000, f"{type(error).__name__}: {error}"[:500]


def command_comparisons(body: Any, expected: dict[str, Any] | None) -> tuple[dict[str, bool], list[str], dict[str, Any] | None]:
    command = body.get("command") if isinstance(body, dict) else None
    if not expected:
        return {}, [], command if isinstance(command, dict) else None
    if not isinstance(command, dict):
        return {field: False for field in expected}, [f"INITIAL_COMMAND_{field.upper()}_MISMATCH" for field in expected], None
    comparisons = {field: command.get(field) == value for field, value in expected.items()}
    failures = [f"INITIAL_COMMAND_{field.upper()}_MISMATCH" for field, correct in comparisons.items() if not correct]
    return comparisons, failures, command


def evaluate_scenario(
    *, client: httpx.Client, engine: Any, base_url: str, run_id: str, repeat: int,
    scenario: dict[str, Any], session_nonce: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session_id = f"cbv2-{session_nonce}-r{repeat}-{scenario['scenario_id']}-{uuid4().hex[:8]}"
    turns: list[dict[str, Any]] = []
    pending_request_id: int | None = None
    scenario_failures: list[str] = []
    scenario_started = time.perf_counter()
    for index, expectation in enumerate(scenario["turns"], start=1):
        status, body, latency_ms, error = post_conversation(client, base_url, session_id, expectation["text"])
        response_valid = isinstance(body, dict) and "message" in body and "session_id" in body
        actual_state = body.get("conversation_state") if isinstance(body, dict) else None
        response_pending_id = body.get("pending_request_id") if isinstance(body, dict) else None
        if isinstance(response_pending_id, int):
            pending_request_id = response_pending_id
        expected_state = expectation.get("expect_state")
        turn_failures: list[str] = []
        if not (200 <= status < 300):
            turn_failures.append("HTTP_ERROR")
        if not response_valid:
            turn_failures.append("RESPONSE_SCHEMA_ERROR")
        if actual_state != expected_state:
            turn_failures.append("STATE_MISMATCH")
        if "expect_message" in expectation and (not isinstance(body, dict) or body.get("message") != expectation["expect_message"]):
            turn_failures.append("MESSAGE_MISMATCH")
        comparisons, command_failures, command = command_comparisons(body, expectation.get("initial_expected"))
        turn_failures.extend(command_failures)
        if expectation.get("metric") == "roof_followup" and turn_failures:
            turn_failures.append("ROOF_SELECTION_MISMATCH")
        if expectation.get("metric") == "confirmation" and turn_failures:
            turn_failures.append("CONFIRMATION_MISMATCH")
        record = {
            "run_id": run_id, "repeat": repeat, "scenario_id": scenario["scenario_id"], "category": scenario["category"],
            "turn_index": index, "session_id": session_id, "input_text": expectation["text"],
            "expected_state": expected_state, "actual_state": actual_state,
            "expected_message": expectation.get("expect_message"), "actual_message": body.get("message") if isinstance(body, dict) else None,
            "pending_request_id": response_pending_id, "expected_command": expectation.get("initial_expected"),
            "actual_command": command, "command_comparisons": comparisons,
            "command_present": isinstance(command, dict), "http_status": status, "latency_ms": round(latency_ms, 3),
            "metric": expectation.get("metric"), "turn_pass": not turn_failures, "failure_reason": sorted(set(turn_failures)),
            "error": error, "response": body,
        }
        turns.append(record)
        scenario_failures.extend(turn_failures)
    snapshot = database_snapshot(engine, pending_request_id)
    expected = scenario["expected"]
    jobs = snapshot["jobs"]
    expected_count = expected["production_job_count"]
    actual_count = len(jobs)
    if snapshot["final_state"] != expected["final_state"]:
        scenario_failures.append("FINAL_STATE_MISMATCH")
    if actual_count != expected_count:
        scenario_failures.append("JOB_COUNT_MISMATCH")
    if snapshot["step_count"] != expected["job_step_count"]:
        scenario_failures.append("STEP_COUNT_MISMATCH")
    if snapshot["event_count"] != expected["job_created_event_count"]:
        scenario_failures.append("EVENT_COUNT_MISMATCH")
    if expected_count:
        if any(job["product_code"] != expected["product_code"] for job in jobs):
            scenario_failures.append("JOB_PRODUCT_MISMATCH")
        if any(job["roof_option_code"] != expected["roof_option_code"] for job in jobs):
            scenario_failures.append("JOB_ROOF_MISMATCH")
        if [job["source_item_index"] for job in jobs] != list(range(1, expected_count + 1)):
            scenario_failures.append("SOURCE_INDEX_MISMATCH")
    duplicate = actual_count > expected_count
    safety_violation = duplicate or (expected_count == 0 and actual_count > 0)
    if duplicate:
        scenario_failures.append("DUPLICATE_JOB_CREATED")
    if expected_count == 0 and actual_count > 0:
        scenario_failures.append("UNEXPECTED_JOB_CREATED")
    scenario_record = {
        "run_id": run_id, "repeat": repeat, "scenario_id": scenario["scenario_id"], "category": scenario["category"],
        "session_id": session_id, "pending_request_id": pending_request_id, "final_pending_state": snapshot["final_state"],
        "expected_final_state": expected["final_state"], "expected_job_count": expected_count, "actual_job_count": actual_count,
        "expected_step_count": expected["job_step_count"], "actual_step_count": snapshot["step_count"],
        "expected_job_created_event_count": expected["job_created_event_count"], "actual_job_created_event_count": snapshot["event_count"],
        "actual_job_product_codes": [job["product_code"] for job in jobs], "actual_job_roof_codes": [job["roof_option_code"] for job in jobs],
        "actual_source_item_indexes": [job["source_item_index"] for job in jobs], "duplicate_job_detected": duplicate,
        "safety_violation": safety_violation, "false_excess_jobs": max(actual_count - expected_count, 0),
        "scenario_latency_ms": round((time.perf_counter() - scenario_started) * 1000, 3),
        "scenario_pass": not scenario_failures, "failure_reason": sorted(set(scenario_failures)),
    }
    return turns, scenario_record


def metric_rate(records: list[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    total = len(records)
    correct = sum(bool(predicate(record)) for record in records)
    return {"correct": correct, "total": total, "rate": ratio(correct, total)}


def latency_summary(samples: list[float]) -> dict[str, Any]:
    return {"samples": len(samples), "mean_ms": statistics.fmean(samples) if samples else None,
            "p50_ms": percentile(samples, 50), "p95_ms": percentile(samples, 95), "max_ms": max(samples) if samples else None}


def summarize(turns: list[dict[str, Any]], scenarios: list[dict[str, Any]], dataset: Dataset) -> dict[str, Any]:
    initial_turns = [row for row in turns if row["turn_index"] == 1]
    followups = [row for row in turns if row["turn_index"] > 1]
    command_turns = [row for row in turns if row["expected_command"]]
    state_turns = list(turns)
    roof_turns = [row for row in turns if row["metric"] == "roof_followup"]
    confirmation_turns = [row for row in turns if row["metric"] == "confirmation"]
    message_turns = [row for row in turns if row["expected_message"] is not None]
    rejection = [row for row in scenarios if row["expected_final_state"] == "REJECTED"]
    materialization = [row for row in scenarios if row["expected_job_count"] > 0]
    categories = {scenario["category"] for scenario in dataset.scenarios}
    grouped: dict[str, Any] = {}
    for category in sorted(categories):
        subset = [row for row in scenarios if row["category"] == category]
        grouped[category] = metric_rate(subset, lambda row: row["scenario_pass"])
    stability: dict[str, int] = {"3/3": 0, "2/3": 0, "1/3": 0, "0/3": 0}
    unstable_ids: list[str] = []
    for source in dataset.scenarios:
        case = [row for row in scenarios if row["scenario_id"] == source["scenario_id"]]
        passes = sum(row["scenario_pass"] for row in case)
        stability[f"{passes}/3"] += 1
        if passes != 3:
            unstable_ids.append(source["scenario_id"])
    return {
        "total_scenario_executions": len(scenarios), "total_turns": len(turns),
        "http_success": metric_rate(turns, lambda row: 200 <= row["http_status"] < 300),
        "conversation_response_parse_success": metric_rate(turns, lambda row: isinstance(row["response"], dict) and "message" in row["response"]),
        "initial_command_exact": metric_rate(command_turns, lambda row: not any(reason.startswith("INITIAL_COMMAND_") for reason in row["failure_reason"])),
        "state_transition_accuracy": metric_rate(state_turns, lambda row: row["expected_state"] == row["actual_state"]),
        "roof_followup_accuracy": metric_rate(roof_turns, lambda row: row["turn_pass"]),
        "confirmation_accuracy": metric_rate(confirmation_turns, lambda row: row["turn_pass"]),
        "deterministic_message_accuracy": metric_rate(message_turns, lambda row: "MESSAGE_MISMATCH" not in row["failure_reason"]),
        "rejection_accuracy": metric_rate(rejection, lambda row: row["scenario_pass"]),
        "job_materialization_exact_accuracy": metric_rate(materialization, lambda row: row["scenario_pass"]),
        "scenario_end_to_end_success": metric_rate(scenarios, lambda row: row["scenario_pass"]),
        "safety_violation": metric_rate(scenarios, lambda row: not row["safety_violation"]),
        "duplicate_job_scenario": metric_rate(scenarios, lambda row: not row["duplicate_job_detected"]),
        "false_excess_production_jobs": sum(row["false_excess_jobs"] for row in scenarios),
        "category_results": grouped, "stability": stability, "unstable_scenario_ids": unstable_ids,
        "initial_turn_latency": latency_summary([row["latency_ms"] for row in initial_turns]),
        "deterministic_followup_latency": latency_summary([row["latency_ms"] for row in followups]),
        "scenario_total_latency": latency_summary([row["scenario_latency_ms"] for row in scenarios]),
    }


def environment_metadata(dataset_path: Path, script_path: Path, database_name: str, base_url: str) -> dict[str, Any]:
    status = run_command(["git", "status", "--porcelain"]) or ""
    diff = run_command(["git", "diff", "--binary", "HEAD"]) or ""
    file_hashes = {path: sha256_file(PROJECT_ROOT / path) for path in CORE_FILES if (PROJECT_ROOT / path).exists()}
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(), "hostname": socket.gethostname(),
        "os": platform.platform(), "kernel": platform.release(), "python_version": sys.version,
        "cpu": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines() if line.startswith("model name")), platform.processor() or None),
        "gpu": run_command(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]),
        "ollama_version": run_command(["ollama", "--version"]), "api_base_url": base_url, "database_name": database_name,
        "git_commit": run_command(["git", "rev-parse", "HEAD"]) or "unavailable", "git_dirty": bool(status),
        "git_status_entries": status.splitlines(), "git_diff_sha256": sha256_bytes(diff.encode()),
        "dataset_path": str(dataset_path.relative_to(PROJECT_ROOT)), "dataset_sha256": sha256_file(dataset_path),
        "benchmark_script_sha256": sha256_file(script_path), "core_file_sha256": file_hashes,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def write_artifacts(results_dir: Path, timestamp: str, turns: list[dict[str, Any]], scenarios: list[dict[str, Any]], summary: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "turns": results_dir / f"conversation_turns_{timestamp}.csv", "scenarios": results_dir / f"conversation_scenarios_{timestamp}.csv",
        "trials": results_dir / f"conversation_trials_{timestamp}.jsonl", "summary": results_dir / f"conversation_summary_{timestamp}.csv",
        "metadata": results_dir / f"conversation_metadata_{timestamp}.json",
    }
    write_csv(paths["turns"], turns)
    write_csv(paths["scenarios"], scenarios)
    with paths["trials"].open("w", encoding="utf-8") as handle:
        for row in {"turns": turns, "scenarios": scenarios}.items():
            for item in row[1]:
                handle.write(json.dumps({"record_type": row[0][:-1], **item}, ensure_ascii=False, default=str) + "\n")
    flat = {key: value for key, value in summary.items() if not isinstance(value, (dict, list))}
    flat["summary_json"] = json.dumps(summary, ensure_ascii=False)
    write_csv(paths["summary"], [flat])
    paths["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return paths


def print_summary(summary: dict[str, Any]) -> None:
    def fmt(metric: dict[str, Any]) -> str:
        return f"{metric['correct']} / {metric['total']} ({metric['rate'] * 100:.1f}%)" if metric["rate"] is not None else "unavailable"
    print("=" * 58)
    print("Conversation Benchmark v2")
    print("=" * 58)
    print(f"HTTP Success                 : {fmt(summary['http_success'])}")
    print(f"Initial Command Exact        : {fmt(summary['initial_command_exact'])}")
    print(f"State Transition Accuracy    : {fmt(summary['state_transition_accuracy'])}")
    print(f"Roof Follow-up Accuracy      : {fmt(summary['roof_followup_accuracy'])}")
    print(f"Confirmation Accuracy        : {fmt(summary['confirmation_accuracy'])}")
    print(f"Deterministic Message Exact  : {fmt(summary['deterministic_message_accuracy'])}")
    print(f"Materialization Exact        : {fmt(summary['job_materialization_exact_accuracy'])}")
    print(f"Scenario End-to-End Success  : {fmt(summary['scenario_end_to_end_success'])}")
    print(f"Safety Violations            : {sum(1 for _ in []) + (summary['total_scenario_executions'] - summary['safety_violation']['correct'])}")
    print(f"3/3 Stability                : {summary['stability']['3/3']} / 30")
    print("=" * 58)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--database-url", default=os.environ.get("BENCHMARK_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    parser.add_argument("--database-name", default=DEFAULT_BENCHMARK_DB)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--label", default="conversation_baseline_v2")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if not args.database_url:
        parser.error("--database-url or BENCHMARK_DATABASE_URL is required")
    dataset_path = args.dataset.resolve()
    dataset = load_dataset(dataset_path)
    assert_benchmark_database(args.database_url, args.database_name)
    engine = create_engine(args.database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = f"{args.label}_{timestamp}"
    metadata = environment_metadata(dataset_path, Path(__file__).resolve(), args.database_name, args.base_url)
    metadata.update({"run_id": run_id, "label": args.label, "dataset_version": dataset.version, "repeats": args.repeats, "alembic_revision": revision, "valid": True})
    preflight_results: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{args.base_url.rstrip('/')}/ai/health")
        metadata["ai_health"] = health.json() if health.is_success else {"status": health.status_code}
        if not health.is_success or not metadata["ai_health"].get("ollama_reachable"):
            raise RuntimeError("/ai/health did not confirm reachable Ollama; formal baseline was not executed.")
        if not args.skip_preflight:
            warmup_scenario = dataset.scenarios[0]
            started = time.perf_counter()
            _, preflight_result = evaluate_scenario(client=client, engine=engine, base_url=args.base_url, run_id=run_id, repeat=0, scenario=warmup_scenario, session_nonce="preflight")
            preflight_results.append(preflight_result)
            metadata["warmup_performed"] = True
            metadata["warmup_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            for chosen in (dataset.scenarios[8], dataset.scenarios[27]):
                _, result = evaluate_scenario(client=client, engine=engine, base_url=args.base_url, run_id=run_id, repeat=0, scenario=chosen, session_nonce="preflight")
                preflight_results.append(result)
            if not all(row["scenario_pass"] for row in preflight_results):
                metadata.update({"valid": False, "invalid_reason": "PREFLIGHT_FAILED", "preflight": preflight_results})
                paths = write_artifacts(DEFAULT_RESULTS_DIR, timestamp, [], [], {"preflight": preflight_results}, metadata)
                print(f"Preflight failed; artifacts: {paths}")
                return 2
        else:
            metadata["warmup_performed"] = False
        metadata["preflight"] = preflight_results
        freeze_sha = sha256_file(dataset_path)
        for repeat in range(1, args.repeats + 1):
            for scenario in dataset.scenarios:
                turn_rows, scenario_row = evaluate_scenario(client=client, engine=engine, base_url=args.base_url, run_id=run_id, repeat=repeat, scenario=scenario, session_nonce=timestamp)
                turns.extend(turn_rows)
                scenarios.append(scenario_row)
        if sha256_file(dataset_path) != freeze_sha:
            metadata.update({"valid": False, "invalid_reason": "DATASET_CHANGED_DURING_FORMAL_RUN"})
    summary = summarize(turns, scenarios, dataset)
    metadata["dataset_sha256_after_run"] = sha256_file(dataset_path)
    metadata["formal_total_scenarios"] = len(scenarios)
    metadata["formal_total_turns"] = len(turns)
    paths = write_artifacts(DEFAULT_RESULTS_DIR, timestamp, turns, scenarios, summary, metadata)
    print_summary(summary)
    failures = [row for row in scenarios if not row["scenario_pass"]]
    if failures:
        print("[FAILED SCENARIOS]")
        for row in failures:
            print(f"{row['scenario_id']} repeat={row['repeat']}: {', '.join(row['failure_reason'])}")
    print("[ARTIFACTS]")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
