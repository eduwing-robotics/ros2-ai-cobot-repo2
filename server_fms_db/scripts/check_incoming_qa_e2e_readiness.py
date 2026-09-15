#!/usr/bin/env python3
"""Read-only safety preflight for a real Incoming QA v0.2 UDP E2E.

This tool intentionally does *not* start FMS/API processes, create database
rows, reconcile transactions, or send a UDP datagram.  It only verifies the
configured target with ``SELECT`` queries and briefly probes whether the local
result UDP address can be bound.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from dataclasses import dataclass
from typing import Callable, Iterable

from sqlalchemy import create_engine, text

from fms_server.incoming_material_qa_udp_transport import IncomingQAUdpRuntimeConfig
from shared.config import get_settings


EXPECTED_BENCHMARK_DATABASE = "smart_factory_benchmark"
PRODUCTION_DATABASE = "smart_factory_db"
EXPECTED_BENCHMARK_REVISION = "20260904_02"


@dataclass(frozen=True, slots=True)
class PreflightInputs:
    """Only the explicit deployment values this checker is allowed to inspect."""

    database_url: str
    cell_transport: str
    vision_host: str | None
    vision_port: int | None
    result_host: str | None
    result_port: int | None
    ack_timeout_seconds: float | None
    max_retries: int | None

    @classmethod
    def from_settings(cls) -> "PreflightInputs":
        settings = get_settings()
        return cls(
            database_url=settings.database_url.strip(),
            cell_transport=settings.cell_transport,
            vision_host=settings.vision_incoming_qa_udp_host,
            vision_port=settings.vision_incoming_qa_udp_port,
            result_host=settings.fms_incoming_qa_result_udp_host,
            result_port=settings.fms_incoming_qa_result_udp_port,
            ack_timeout_seconds=settings.incoming_qa_udp_ack_timeout_seconds,
            max_retries=settings.incoming_qa_udp_max_retries,
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    def render(self) -> str:
        lines = [
            f"[{'PASS' if check.passed else 'FAIL'}] {check.message}"
            for check in self.checks
        ]
        lines.append(
            "READY_FOR_INCOMING_QA_E2E"
            if self.ready
            else "NOT_READY_FOR_INCOMING_QA_E2E"
        )
        return "\n".join(lines)


DatabaseProbe = Callable[[str], tuple[str, str | None]]
LocalIPv4Provider = Callable[[], set[str]]
PortProbe = Callable[[str, int], bool]


def _read_database_identity(database_url: str) -> tuple[str, str | None]:
    """Use only SELECT statements and never echo a URL or its credentials."""

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database_name = connection.scalar(text("SELECT current_database()"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if not isinstance(database_name, str):
            raise RuntimeError("current_database() returned no database name")
        return database_name, revision if isinstance(revision, str) else None
    finally:
        engine.dispose()


def _local_ipv4_addresses() -> set[str]:
    """Return local interface addresses without opening or sending a packet."""

    addresses = {"127.0.0.1"}
    for hostname in {socket.gethostname(), socket.getfqdn(), "localhost"}:
        try:
            addresses.update(
                info[4][0]
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET)
            )
        except socket.gaierror:
            continue

    # psutil is optional in this application.  When available it is the most
    # reliable cross-interface source; the stdlib hostname lookup remains a
    # safe fallback for minimal deployments.
    try:
        import psutil  # type: ignore[import-not-found]

        for interface in psutil.net_if_addrs().values():
            addresses.update(
                address.address
                for address in interface
                if address.family == socket.AF_INET
            )
    except ImportError:
        pass
    return addresses


def _is_local_ipv4(host: str, addresses: Iterable[str]) -> bool:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed.version == 4 and not parsed.is_unspecified and host in set(addresses)


def _result_port_is_available(host: str, port: int) -> bool:
    """Probe a local UDP bind and close immediately; this sends no datagram."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _configured(value: object) -> bool:
    return value is not None and value != ""


def run_preflight(
    inputs: PreflightInputs,
    *,
    database_probe: DatabaseProbe = _read_database_identity,
    local_ipv4_provider: LocalIPv4Provider = _local_ipv4_addresses,
    port_probe: PortProbe = _result_port_is_available,
) -> PreflightReport:
    """Evaluate all checks without writes, process startup, or UDP transmission."""

    checks: list[CheckResult] = []
    database_url_configured = bool(inputs.database_url.strip())
    checks.append(CheckResult(database_url_configured, "DATABASE_URL configured"))

    if database_url_configured:
        try:
            database_name, revision = database_probe(inputs.database_url)
        except Exception as exc:  # The failure is intentionally credential-safe.
            checks.append(CheckResult(False, f"read-only database check failed ({type(exc).__name__})"))
            database_name, revision = None, None
        else:
            if database_name == PRODUCTION_DATABASE:
                checks.append(CheckResult(False, "current_database=smart_factory_db is prohibited"))
            elif database_name == EXPECTED_BENCHMARK_DATABASE:
                checks.append(CheckResult(True, "current_database=smart_factory_benchmark"))
            else:
                checks.append(
                    CheckResult(False, f"current_database={database_name!r}; expected smart_factory_benchmark")
                )
            if revision == EXPECTED_BENCHMARK_REVISION:
                checks.append(CheckResult(True, f"DB revision={revision}"))
            elif revision is None:
                checks.append(CheckResult(False, "Alembic current revision unavailable"))
            else:
                checks.append(
                    CheckResult(False, f"DB revision={revision}; expected {EXPECTED_BENCHMARK_REVISION}")
                )
    else:
        checks.append(CheckResult(False, "current_database cannot be checked without DATABASE_URL"))
        checks.append(CheckResult(False, "Alembic current revision cannot be checked without DATABASE_URL"))

    cell_transport_is_fake = inputs.cell_transport == "fake"
    checks.append(
        CheckResult(
            cell_transport_is_fake,
            "CELL_TRANSPORT=fake" if cell_transport_is_fake else "CELL_TRANSPORT must be fake",
        )
    )

    required_fields = (
        ("Vision Incoming QA host", inputs.vision_host),
        ("Vision request port", inputs.vision_port),
        ("Server result host", inputs.result_host),
        ("Server result port", inputs.result_port),
    )
    for label, value in required_fields:
        checks.append(
            CheckResult(
                _configured(value),
                f"{label}={value}" if _configured(value) else f"{label} is not configured",
            )
        )

    result_host_is_local = False
    if isinstance(inputs.result_host, str) and inputs.result_host.strip():
        result_host_is_local = _is_local_ipv4(inputs.result_host, local_ipv4_provider())
        checks.append(
            CheckResult(
                result_host_is_local,
                f"FMS result host={inputs.result_host} is a local IPv4"
                if result_host_is_local
                else f"FMS result host={inputs.result_host} is not a local IPv4",
            )
        )
    else:
        checks.append(CheckResult(False, "FMS result host cannot be verified without configuration"))

    port_is_valid = isinstance(inputs.result_port, int) and 1 <= inputs.result_port <= 65535
    if not port_is_valid:
        checks.append(CheckResult(False, "FMS result UDP port must be in 1..65535"))
    elif result_host_is_local and isinstance(inputs.result_host, str):
        port_available = port_probe(inputs.result_host, inputs.result_port)
        checks.append(
            CheckResult(
                port_available,
                "Result UDP port available" if port_available else "Result UDP port is already bound",
            )
        )
    else:
        checks.append(CheckResult(False, "Result UDP port cannot be checked until result host is local"))

    try:
        IncomingQAUdpRuntimeConfig(
            vision_host=inputs.vision_host or "",
            vision_port=inputs.vision_port if isinstance(inputs.vision_port, int) else -1,
            result_host=inputs.result_host or "",
            result_port=inputs.result_port if isinstance(inputs.result_port, int) else -1,
            ack_timeout_seconds=(
                inputs.ack_timeout_seconds
                if isinstance(inputs.ack_timeout_seconds, (int, float))
                else 0
            ),
            max_retries=inputs.max_retries if isinstance(inputs.max_retries, int) else -1,
        )
    except Exception:
        checks.append(CheckResult(False, "Incoming QA v0.2 UDP configuration incomplete or invalid"))
    else:
        checks.append(CheckResult(True, "Incoming QA v0.2 UDP configuration complete"))

    checks.append(CheckResult(True, "PRE_ROOF configuration is not used by this Incoming QA preflight"))
    return PreflightReport(checks=tuple(checks))


def main() -> int:
    try:
        report = run_preflight(PreflightInputs.from_settings())
    except Exception as exc:
        # Configuration loading errors are still fail-closed and do not reveal
        # database credentials or start a runtime.
        report = PreflightReport((CheckResult(False, f"preflight initialization failed ({type(exc).__name__})"),))
    print(report.render())
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(main())
