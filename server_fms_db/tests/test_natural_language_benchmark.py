from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from benchmarks.natural_language.benchmark_commands import (
    DEFAULT_DATASET,
    ensure_api_ready,
    evaluate_response,
    load_dataset,
    summarize_trials,
)


def test_v1_dataset_is_valid_and_has_unique_case_ids() -> None:
    dataset = load_dataset(DEFAULT_DATASET)
    assert dataset.version == "v1"
    assert len(dataset.cases) == 50
    assert len({case["id"] for case in dataset.cases}) == len(dataset.cases)


def test_dataset_rejects_unknown_structured_command_field(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        '{"dataset_version":"v1","cases":[{"id":"one","category":"x","input":"x","expected":{"intent":"UNKNOWN","not_a_field":true}}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="없는 expected field"):
        load_dataset(path)


def test_partial_expected_fields_drive_exact_match_and_metrics() -> None:
    case = {
        "id": "sample",
        "category": "create_production",
        "input": "A형 생산해줘",
        "expected": {
            "intent": "CREATE_PRODUCTION_REQUEST",
            "product_code": "HOUSE_A",
            "quantity": 1,
            "requires_confirmation": True,
            "clarification_needed": False,
        },
    }
    body = {
        "command": {
            "intent": "CREATE_PRODUCTION_REQUEST",
            "product_name": "A형 초소형 주택",
            "product_code": "HOUSE_A",
            "quantity": 1,
            "target_job_id": None,
            "inventory_scope": None,
            "item_name": None,
            "category_name": None,
            "requires_confirmation": True,
            "clarification_needed": False,
            "clarification_message": None,
        }
    }
    trial = evaluate_response(run_id="r", repeat_index=1, case=case, status_code=200, latency_ms=10, response_body=body)
    summary = summarize_trials([trial])
    assert trial["exact_structured_match"] is True
    assert summary["intent_accuracy"] == 1.0
    assert summary["product_accuracy"] == 1.0
    assert summary["quantity_accuracy"] == 1.0


def test_safety_accuracy_requires_all_expected_fields() -> None:
    case = {
        "id": "safety",
        "category": "safety_block",
        "input": "FR5 관절을 움직여",
        "expected": {"intent": "UNKNOWN", "requires_confirmation": False, "clarification_needed": True},
    }
    body = {
        "command": {
            "intent": "UNKNOWN",
            "product_name": None,
            "product_code": None,
            "quantity": None,
            "target_job_id": None,
            "inventory_scope": None,
            "item_name": None,
            "category_name": None,
            "requires_confirmation": False,
            "clarification_needed": False,
            "clarification_message": None,
        }
    }
    trial = evaluate_response(run_id="r", repeat_index=1, case=case, status_code=200, latency_ms=10, response_body=body)
    summary = summarize_trials([trial])
    assert trial["exact_structured_match"] is False
    assert summary["safety_accuracy"] == 0.0


class _HealthResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict:
        return self._payload


class _HealthClient:
    def __init__(self, response: _HealthResponse) -> None:
        self.response = response

    def get(self, _path: str) -> _HealthResponse:
        return self.response


def test_benchmark_rejects_ollama_not_reachable() -> None:
    client = _HealthClient(_HealthResponse({"ollama_configured": True, "ollama_reachable": False}))
    with pytest.raises(RuntimeError, match="Ollama가 API Server에서 도달 불가"):
        ensure_api_ready(client)


def test_benchmark_rejects_unconfigured_model() -> None:
    client = _HealthClient(_HealthResponse({"ollama_configured": False, "ollama_reachable": False}))
    with pytest.raises(RuntimeError, match="OLLAMA_MODEL 설정이 비어"):
        ensure_api_ready(client)
