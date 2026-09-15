"""End-to-end benchmark for the existing /ai/interpret command pipeline.

This tool deliberately uses HTTP instead of importing CommandInterpreter.  It does
not alter production state: /ai/interpret only interprets commands (and may perform
read-only inventory lookups for QUERY_INVENTORY).
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from typing import Any

import httpx
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api_server.services.llm_service import SYSTEM_PROMPT
from shared.config import get_settings
from shared.enums.ai import Intent
from shared.schemas.ai import StructuredCommand


DEFAULT_DATASET = Path(__file__).resolve().with_name("command_eval_dataset.json")
DEFAULT_RESULTS_DIR = Path(__file__).resolve().with_name("results")
COMMAND_FIELDS = tuple(StructuredCommand.model_fields)
MEASURED_FIELD_LABELS = {
    "intent": "intent",
    "product_code": "product",
    "quantity": "quantity",
    "clarification_needed": "clarification",
    "requires_confirmation": "confirmation",
    "target_job_id": "job_target",
    "inventory_scope": "inventory_parameter",
    "item_name": "inventory_parameter",
    "category_name": "inventory_parameter",
}


@dataclass(frozen=True)
class Dataset:
    version: str
    cases: list[dict[str, Any]]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run_command(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(args, cwd=PROJECT_ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_metadata() -> dict[str, Any]:
    status = run_command(["git", "status", "--porcelain"]) or ""
    diff = run_command(["git", "diff", "--binary", "HEAD"]) or ""
    untracked = run_command(["git", "ls-files", "--others", "--exclude-standard"]) or ""
    return {
        "git_commit": run_command(["git", "rev-parse", "HEAD"]) or "unavailable",
        "git_dirty": bool(status),
        "git_status_entries": status.splitlines(),
        "working_tree_diff_sha256": sha256_bytes(diff.encode("utf-8")),
        "untracked_paths": untracked.splitlines(),
    }


def cpu_info() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unavailable"


def gpu_info() -> str | None:
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return output or None


def load_dataset(path: Path) -> Dataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("dataset_version")
    cases = raw.get("cases")
    if not isinstance(version, str) or not version:
        raise ValueError("dataset_version이 필요합니다.")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases는 비어 있지 않은 배열이어야 합니다.")

    ids: set[str] = set()
    allowed_expected = set(COMMAND_FIELDS)
    valid_intents = {intent.value for intent in Intent}
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"case {index}은 객체여야 합니다.")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case {index}의 id가 필요합니다.")
        if case_id in ids:
            raise ValueError(f"중복 case id: {case_id}")
        ids.add(case_id)
        if not isinstance(case.get("category"), str) or not case["category"]:
            raise ValueError(f"{case_id}: category가 필요합니다.")
        if not isinstance(case.get("input"), str) or not case["input"].strip():
            raise ValueError(f"{case_id}: input이 필요합니다.")
        expected = case.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise ValueError(f"{case_id}: expected 객체가 필요합니다.")
        unknown_fields = set(expected) - allowed_expected
        if unknown_fields:
            raise ValueError(f"{case_id}: 현재 StructuredCommand에 없는 expected field: {sorted(unknown_fields)}")
        if expected.get("intent") not in valid_intents:
            raise ValueError(f"{case_id}: 유효하지 않은 expected intent: {expected.get('intent')}")
    return Dataset(version=version, cases=cases)


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


def compact_error(response: httpx.Response | None, error: Exception | None) -> str:
    if error is not None:
        return f"{type(error).__name__}: {error}"[:500]
    if response is None:
        return "response unavailable"
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = response.text
    return str(detail or f"HTTP {response.status_code}")[:500]


def clean_response_for_artifact(value: Any) -> Any:
    """Avoid persisting debug-only raw model output in benchmark artifacts."""
    if isinstance(value, dict):
        return {key: clean_response_for_artifact(item) for key, item in value.items() if key != "raw_model_output"}
    if isinstance(value, list):
        return [clean_response_for_artifact(item) for item in value]
    return value


def evaluate_response(
    *,
    run_id: str,
    repeat_index: int,
    case: dict[str, Any],
    status_code: int,
    latency_ms: float,
    response_body: Any,
    error: str = "",
) -> dict[str, Any]:
    expected = case["expected"]
    http_success = 200 <= status_code < 300
    actual_command: dict[str, Any] | None = None
    structured_success = False
    if http_success and isinstance(response_body, dict) and isinstance(response_body.get("command"), dict):
        try:
            actual_command = StructuredCommand.model_validate(response_body["command"]).model_dump(mode="json")
            structured_success = True
        except ValidationError as validation_error:
            error = error or f"response StructuredCommand validation failed: {validation_error.errors()[0]['msg']}"

    comparisons: dict[str, bool | None] = {}
    mismatches: list[str] = []
    for field, expected_value in expected.items():
        actual_value = actual_command.get(field) if actual_command is not None else None
        correct = actual_command is not None and actual_value == expected_value
        comparisons[field] = correct
        if not correct:
            mismatches.append(field)

    trial = {
        "run_id": run_id,
        "repeat_index": repeat_index,
        "case_id": case["id"],
        "category": case["category"],
        "input_text": case["input"],
        "expected": expected,
        "actual_command": actual_command,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 3),
        "http_success": http_success,
        "structured_success": structured_success,
        "comparisons": comparisons,
        "exact_structured_match": structured_success and not mismatches,
        "mismatch_fields": mismatches,
        "error": error,
        "actual_response": clean_response_for_artifact(response_body),
    }
    return trial


def make_trial_csv_row(trial: dict[str, Any]) -> dict[str, Any]:
    expected, actual, comparisons = trial["expected"], trial["actual_command"] or {}, trial["comparisons"]
    row: dict[str, Any] = {
        "run_id": trial["run_id"],
        "repeat_index": trial["repeat_index"],
        "case_id": trial["case_id"],
        "category": trial["category"],
        "input_text": trial["input_text"],
        "status_code": trial["status_code"],
        "latency_ms": trial["latency_ms"],
        "http_success": trial["http_success"],
        "structured_success": trial["structured_success"],
        "exact_structured_match": trial["exact_structured_match"],
        "mismatch_fields": ",".join(trial["mismatch_fields"]),
        "error": trial["error"],
    }
    for field in COMMAND_FIELDS:
        row[f"expected_{field}"] = json.dumps(expected[field], ensure_ascii=False) if field in expected else ""
        row[f"actual_{field}"] = json.dumps(actual.get(field), ensure_ascii=False)
        row[f"{field}_correct"] = comparisons.get(field, "")
    return row


def ratio(correct: int, total: int) -> float | None:
    return correct / total if total else None


def summarize_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trials)
    result: dict[str, Any] = {
        "total_cases": total,
        "http_success_count": sum(trial["http_success"] for trial in trials),
        "structured_success_count": sum(trial["structured_success"] for trial in trials),
        "exact_command_correct": sum(trial["exact_structured_match"] for trial in trials),
        "exact_command_total": total,
    }
    result["http_success_rate"] = ratio(result["http_success_count"], total)
    result["structured_success_rate"] = ratio(result["structured_success_count"], total)
    result["exact_command_accuracy"] = ratio(result["exact_command_correct"], total)

    for metric in sorted(set(MEASURED_FIELD_LABELS.values())):
        result[f"{metric}_correct"] = 0
        result[f"{metric}_total"] = 0
    for trial in trials:
        for field, compared in trial["comparisons"].items():
            metric = MEASURED_FIELD_LABELS.get(field)
            expected_value = trial["expected"].get(field)
            # Optional entity/parameter metrics use only meaningful expected values.
            # A null still participates in this case's exact partial-command match.
            if metric is None or (field in {"product_code", "quantity", "target_job_id", "inventory_scope", "item_name", "category_name"} and expected_value is None):
                continue
            result[f"{metric}_total"] += 1
            result[f"{metric}_correct"] += int(bool(compared))
    for metric in sorted(set(MEASURED_FIELD_LABELS.values())):
        result[f"{metric}_accuracy"] = ratio(result[f"{metric}_correct"], result[f"{metric}_total"])

    safety_trials = [trial for trial in trials if trial["category"] == "safety_block"]
    result["safety_correct"] = sum(trial["exact_structured_match"] for trial in safety_trials)
    result["safety_total"] = len(safety_trials)
    result["safety_accuracy"] = ratio(result["safety_correct"], result["safety_total"])

    latencies = [trial["latency_ms"] for trial in trials]
    result.update(
        mean_latency_ms=statistics.fmean(latencies) if latencies else None,
        p50_latency_ms=percentile(latencies, 50),
        p95_latency_ms=percentile(latencies, 95),
        p99_latency_ms=percentile(latencies, 99),
        min_latency_ms=min(latencies) if latencies else None,
        max_latency_ms=max(latencies) if latencies else None,
    )
    return result


def category_summary(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for category in sorted({trial["category"] for trial in trials}):
        subset = [trial for trial in trials if trial["category"] == category]
        values[category] = {
            "total": len(subset),
            "structured_success": sum(trial["structured_success"] for trial in subset),
            "exact_correct": sum(trial["exact_structured_match"] for trial in subset),
            "exact_accuracy": ratio(sum(trial["exact_structured_match"] for trial in subset), len(subset)),
        }
    return values


def case_stability(trials: list[dict[str, Any]], repeats: int) -> dict[str, Any] | None:
    if repeats <= 1:
        return None
    by_case: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        by_case.setdefault(trial["case_id"], []).append(trial)
    stable = 0
    for values in by_case.values():
        signatures = {json.dumps(value["actual_command"], ensure_ascii=False, sort_keys=True) for value in values}
        if len(signatures) == 1:
            stable += 1
    return {"stable_case_count": stable, "case_count": len(by_case), "stable_case_rate": ratio(stable, len(by_case))}


def request_interpret(client: httpx.Client, text: str) -> tuple[int, float, Any, str]:
    started = time.perf_counter()
    try:
        response = client.post("/ai/interpret", json={"text": text})
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body: Any = response.json()
        except ValueError:
            body = {"detail": "non-JSON response"}
        error = "" if response.is_success else compact_error(response, None)
        return response.status_code, latency_ms, body, error
    except httpx.HTTPError as error:
        latency_ms = (time.perf_counter() - started) * 1000
        return 0, latency_ms, None, compact_error(None, error)


def ensure_api_ready(client: httpx.Client) -> dict[str, Any]:
    try:
        response = client.get("/ai/health")
    except httpx.HTTPError as error:
        raise RuntimeError(f"API Server에 연결할 수 없습니다: {type(error).__name__}: {error}") from error
    if not response.is_success:
        raise RuntimeError(f"/ai/health가 HTTP {response.status_code}를 반환했습니다.")
    try:
        health = response.json()
    except ValueError as error:
        raise RuntimeError("/ai/health가 JSON 응답을 반환하지 않았습니다.") from error
    if not health.get("ollama_configured"):
        raise RuntimeError("API Server의 OLLAMA_MODEL 설정이 비어 있습니다.")
    if not health.get("ollama_reachable"):
        raise RuntimeError("Ollama가 API Server에서 도달 불가입니다.")
    return health


def write_jsonl(path: Path, trials: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as file:
        for trial in trials:
            file.write(json.dumps(trial, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def display_ratio(correct: int, total: int) -> str:
    value = ratio(correct, total)
    return f"{correct} / {total} ({value * 100:.1f}%)" if value is not None else "N/A"


def print_summary(summary: dict[str, Any], *, model: str, dataset: Dataset, first_request_ms: float | None) -> None:
    print("\n==================================================")
    print(f"Natural Language Command Baseline {dataset.version}")
    print("==================================================")
    print(f"Model                : {model}")
    print(f"Dataset              : {dataset.version}")
    print(f"Cases                : {summary['total_cases']}")
    print()
    print(f"HTTP Success         : {display_ratio(summary['http_success_count'], summary['total_cases'])}")
    print(f"Structured Success   : {display_ratio(summary['structured_success_count'], summary['total_cases'])}")
    print(f"Intent Accuracy      : {display_ratio(summary['intent_correct'], summary['intent_total'])}")
    print(f"Product Accuracy     : {display_ratio(summary['product_correct'], summary['product_total'])}")
    print(f"Quantity Accuracy    : {display_ratio(summary['quantity_correct'], summary['quantity_total'])}")
    print(f"Clarification        : {display_ratio(summary['clarification_correct'], summary['clarification_total'])}")
    print(f"Confirmation         : {display_ratio(summary['confirmation_correct'], summary['confirmation_total'])}")
    print(f"Safety Block         : {display_ratio(summary['safety_correct'], summary['safety_total'])}")
    print(f"Exact Command        : {display_ratio(summary['exact_command_correct'], summary['exact_command_total'])}")
    print()
    print(f"Mean Latency         : {summary['mean_latency_ms']:.1f} ms")
    print(f"P50 Latency          : {summary['p50_latency_ms']:.1f} ms")
    print(f"P95 Latency          : {summary['p95_latency_ms']:.1f} ms")
    print(f"First Request        : {first_request_ms:.1f} ms" if first_request_ms is not None else "First Request        : unavailable")
    print("==================================================")


def print_failures(trials: list[dict[str, Any]]) -> None:
    failed = [trial for trial in trials if not trial["exact_structured_match"]]
    if not failed:
        print("\n[FAILED CASES]\n없음")
        return
    print("\n[FAILED CASES]")
    for trial in failed:
        print(f"\n{trial['case_id']} ({trial['category']})")
        print(f"Input: {trial['input_text']}")
        print(f"Expected: {json.dumps(trial['expected'], ensure_ascii=False)}")
        print(f"Actual: {json.dumps(trial['actual_command'], ensure_ascii=False)}")
        print(f"Mismatch: {', '.join(trial['mismatch_fields']) or trial['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the real HTTP /ai/interpret pipeline against the fixed dataset.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Running API Server base URL")
    parser.add_argument("--label", default="baseline_v1", help="Human-readable run label")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="Versioned dataset JSON path")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="Directory for timestamped result files")
    parser.add_argument("--timeout", type=float, default=75.0, help="HTTP client timeout in seconds; does not alter API/Ollama settings")
    parser.add_argument("--warmup", type=int, default=3, help="Warm-up interpret requests after the separate first request")
    parser.add_argument("--repeats", type=int, default=1, help="Serial repeats of each fixed dataset case")
    args = parser.parse_args()
    if args.repeats < 1 or args.warmup < 0 or args.timeout <= 0:
        parser.error("--repeats는 1 이상, --warmup은 0 이상, --timeout은 0 초과여야 합니다.")

    try:
        dataset = load_dataset(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Dataset validation failed: {error}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    measured_at = datetime.now(timezone.utc)
    timestamp = measured_at.strftime("%Y%m%d_%H%M%S_%f")
    run_id = f"{args.label}_{timestamp}"
    settings = get_settings()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.timeout) as client:
        try:
            health = ensure_api_ready(client)
        except RuntimeError as error:
            print(f"Benchmark not executed: {error}", file=sys.stderr)
            print("API Server 예: .venv/bin/python -m uvicorn api_server.main:app --host 127.0.0.1 --port 8000", file=sys.stderr)
            return 2

        first_status, first_latency, first_body, first_error = request_interpret(client, "현재 생산 상태 알려줘")
        warmup_trials: list[dict[str, Any]] = []
        for _ in range(args.warmup):
            status, latency, body, error = request_interpret(client, "현재 생산 상태 알려줘")
            warmup_trials.append({"status_code": status, "latency_ms": round(latency, 3), "error": error, "structured_response": isinstance(body, dict) and isinstance(body.get("command"), dict)})

        trials: list[dict[str, Any]] = []
        for repeat_index in range(1, args.repeats + 1):
            for case in dataset.cases:
                status, latency, body, error = request_interpret(client, case["input"])
                trials.append(evaluate_response(
                    run_id=run_id,
                    repeat_index=repeat_index,
                    case=case,
                    status_code=status,
                    latency_ms=latency,
                    response_body=body,
                    error=error,
                ))

    summary = summarize_trials(trials)
    categories = category_summary(trials)
    metadata = {
        "run_id": run_id,
        "measured_at": measured_at.isoformat(),
        "label": args.label,
        "api_base_url": args.base_url.rstrip("/"),
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": health.get("ollama_model") or settings.ollama_model,
        "ollama_options_observed_in_source": {"stream": False, "format": "json", "temperature": 0, "num_predict": 160},
        "ollama_timeout_seconds": settings.ollama_timeout_seconds,
        "http_client_timeout_seconds": args.timeout,
        "dataset_file": str(args.dataset.resolve()),
        "dataset_version": dataset.version,
        "dataset_case_count": len(dataset.cases),
        "dataset_sha256": sha256_file(args.dataset),
        "system_prompt_sha256": sha256_bytes(SYSTEM_PROMPT.encode("utf-8")),
        "benchmark_script_sha256": sha256_file(Path(__file__).resolve()),
        "tracked_file_sha256": {
            str(path.relative_to(PROJECT_ROOT)): sha256_file(path)
            for path in (
                PROJECT_ROOT / "api_server/services/command_interpreter.py",
                PROJECT_ROOT / "api_server/services/llm_service.py",
                PROJECT_ROOT / "shared/schemas/ai.py",
                PROJECT_ROOT / "shared/enums/ai.py",
                PROJECT_ROOT / "api_server/services/temporary_product_catalog.py",
            )
        },
        "python_version": sys.version.replace("\n", " "),
        "os": platform.platform(),
        "cpu": cpu_info(),
        "gpu": gpu_info(),
        "repeats": args.repeats,
        "first_request": {"input": "현재 생산 상태 알려줘", "status_code": first_status, "latency_ms": round(first_latency, 3), "error": first_error, "structured_response": isinstance(first_body, dict) and isinstance(first_body.get("command"), dict)},
        "warmup": {"count": args.warmup, "trials": warmup_trials, "p50_latency_ms": percentile([item["latency_ms"] for item in warmup_trials], 50), "p95_latency_ms": percentile([item["latency_ms"] for item in warmup_trials], 95)},
        "raw_llm_json_direct_valid_rate": "unavailable in v1: /ai/interpret exposes only final validated response",
        "repair_retry_rate": "unavailable in v1: retry count is not exposed by the API",
        "category_summary": categories,
        "case_stability": case_stability(trials, args.repeats),
        **git_metadata(),
    }

    trial_csv = args.out_dir / f"nl_command_trials_{timestamp}.csv"
    trial_jsonl = args.out_dir / f"nl_command_trials_{timestamp}.jsonl"
    summary_csv = args.out_dir / f"nl_command_summary_{timestamp}.csv"
    metadata_json = args.out_dir / f"nl_command_metadata_{timestamp}.json"
    csv_rows = [make_trial_csv_row(trial) for trial in trials]
    csv_fields = list(csv_rows[0]) if csv_rows else []
    write_csv(trial_csv, csv_rows, csv_fields)
    write_jsonl(trial_jsonl, trials)
    summary_row = {"run_id": run_id, "measured_at": measured_at.isoformat(), "label": args.label, "dataset_version": dataset.version, **summary}
    write_csv(summary_csv, [summary_row], list(summary_row))
    with metadata_json.open("x", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print_summary(summary, model=str(metadata["ollama_model"]), dataset=dataset, first_request_ms=first_latency)
    print_failures(trials)
    print("\n[RESULT FILES]")
    for path in (trial_csv, trial_jsonl, summary_csv, metadata_json):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
