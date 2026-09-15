from __future__ import annotations

import socket
from dataclasses import replace

import pytest

from scripts.check_incoming_qa_e2e_readiness import (
    EXPECTED_BENCHMARK_DATABASE,
    EXPECTED_BENCHMARK_REVISION,
    PRODUCTION_DATABASE,
    PreflightInputs,
    run_preflight,
)


LOCAL_HOST = "192.168.20.20"


def _benchmark_probe(_: str) -> tuple[str, str | None]:
    return EXPECTED_BENCHMARK_DATABASE, EXPECTED_BENCHMARK_REVISION


def _local_ipv4s() -> set[str]:
    return {LOCAL_HOST}


def _available_port(_: str, __: int) -> bool:
    return True


@pytest.fixture
def happy_inputs() -> PreflightInputs:
    return PreflightInputs(
        database_url="postgresql+psycopg://test:secret@localhost/smart_factory_benchmark",
        cell_transport="fake",
        vision_host="192.168.20.30",
        vision_port=20051,
        result_host=LOCAL_HOST,
        result_port=20052,
        ack_timeout_seconds=1.0,
        max_retries=2,
    )


def _run(inputs: PreflightInputs, *, database_probe=_benchmark_probe, port_probe=_available_port):
    return run_preflight(
        inputs,
        database_probe=database_probe,
        local_ipv4_provider=_local_ipv4s,
        port_probe=port_probe,
    )


def test_happy_benchmark_preflight_is_ready(happy_inputs: PreflightInputs) -> None:
    report = _run(happy_inputs)

    assert report.ready is True
    output = report.render()
    assert "[PASS] current_database=smart_factory_benchmark" in output
    assert "[PASS] Result UDP port available" in output
    assert output.endswith("READY_FOR_INCOMING_QA_E2E")
    assert "secret" not in output


def test_production_database_is_fail_closed(happy_inputs: PreflightInputs) -> None:
    report = _run(happy_inputs, database_probe=lambda _: (PRODUCTION_DATABASE, EXPECTED_BENCHMARK_REVISION))

    assert report.ready is False
    assert "[FAIL] current_database=smart_factory_db is prohibited" in report.render()


def test_real_cell_transport_is_rejected(happy_inputs: PreflightInputs) -> None:
    report = _run(replace(happy_inputs, cell_transport="ros2"))

    assert report.ready is False
    assert "[FAIL] CELL_TRANSPORT must be fake" in report.render()


@pytest.mark.parametrize(
    "field",
    ("vision_host", "vision_port", "result_host", "result_port"),
)
def test_each_required_udp_endpoint_value_is_fail_closed(
    happy_inputs: PreflightInputs, field: str
) -> None:
    report = _run(replace(happy_inputs, **{field: None}))

    assert report.ready is False
    assert "Incoming QA v0.2 UDP configuration incomplete or invalid" in report.render()


def test_occupied_result_port_is_rejected(happy_inputs: PreflightInputs) -> None:
    report = _run(happy_inputs, port_probe=lambda _host, _port: False)

    assert report.ready is False
    assert "[FAIL] Result UDP port is already bound" in report.render()


def test_wrong_result_host_is_rejected(happy_inputs: PreflightInputs) -> None:
    report = _run(replace(happy_inputs, result_host="192.168.20.99"))

    assert report.ready is False
    assert "not a local IPv4" in report.render()


def test_revision_mismatch_is_rejected(happy_inputs: PreflightInputs) -> None:
    report = _run(happy_inputs, database_probe=lambda _: (EXPECTED_BENCHMARK_DATABASE, "20260904_01"))

    assert report.ready is False
    assert "expected 20260904_02" in report.render()


def test_missing_database_url_never_attempts_database_probe(happy_inputs: PreflightInputs) -> None:
    def unexpected_probe(_: str) -> tuple[str, str | None]:
        raise AssertionError("database probe must not run")

    report = _run(replace(happy_inputs, database_url=""), database_probe=unexpected_probe)

    assert report.ready is False
    assert "DATABASE_URL configured" in report.render()


def test_actual_udp_bind_probe_detects_an_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        inputs = PreflightInputs(
            database_url="postgresql+psycopg://test:secret@localhost/smart_factory_benchmark",
            cell_transport="fake",
            vision_host="127.0.0.1",
            vision_port=20051,
            result_host="127.0.0.1",
            result_port=port,
            ack_timeout_seconds=1.0,
            max_retries=2,
        )
        report = run_preflight(
            inputs,
            database_probe=_benchmark_probe,
            local_ipv4_provider=lambda: {"127.0.0.1"},
        )

    assert report.ready is False
    assert "Result UDP port is already bound" in report.render()
