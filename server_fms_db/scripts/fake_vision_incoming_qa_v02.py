#!/usr/bin/env python3
"""Loopback-only Fake Vision peer for Incoming QA v0.2 UDP development.

It is intentionally a development peer, not an FMS component: it does not
connect to PostgreSQL, start API/FMS, create jobs, or derive inspection work.
It validates a datagram request, returns the ACK to that packet's source port,
and sends the terminal result to the explicitly configured FMS result port.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from shared.schemas.vision import (
    IncomingQAAckV02,
    IncomingQAMessageType,
    IncomingQARequestItemV02,
    IncomingQARequestV02,
    IncomingQAResultItemV02,
    IncomingQAResultV02,
)
from shared.vision_recipe_mapping import IncomingQAInspectionMode


logger = logging.getLogger(__name__)


class FakeVisionScenario(StrEnum):
    ALL_PASS = "all-pass"
    BASE_FAIL = "base-fail"
    BASE_NOT_EVALUATED = "base-not-evaluated"
    B02_FAIL = "b02-fail"


class FakeVisionConfigurationError(ValueError):
    """A simulator endpoint could reach a non-loopback network without opt-in."""


def _is_loopback_host(host: str) -> bool:
    """Resolve a host and require every IPv4/IPv6 address to be loopback."""

    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
        }
    except (socket.gaierror, ValueError):
        return False
    return bool(addresses) and all(address.is_loopback for address in addresses)


@dataclass(frozen=True, slots=True)
class FakeVisionIncomingQAConfig:
    host: str
    request_port: int
    server_result_host: str
    server_result_port: int
    scenario: FakeVisionScenario = FakeVisionScenario.ALL_PASS
    allow_non_loopback: bool = False
    drop_first_ack: bool = False
    duplicate_ack: bool = False
    duplicate_result: bool = False
    result_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.host.strip() or not self.server_result_host.strip():
            raise FakeVisionConfigurationError("Fake Vision bind and server result hosts are required.")
        if not (0 <= self.request_port <= 65535 and 1 <= self.server_result_port <= 65535):
            raise FakeVisionConfigurationError("Request port must be 0..65535 and server result port 1..65535.")
        if self.result_delay_seconds < 0:
            raise FakeVisionConfigurationError("Result delay cannot be negative.")
        if not self.allow_non_loopback:
            for label, host in (("Fake Vision bind", self.host), ("Server result", self.server_result_host)):
                if not _is_loopback_host(host):
                    raise FakeVisionConfigurationError(
                        f"{label} host {host!r} is not loopback; pass --allow-non-loopback explicitly."
                    )


@dataclass(frozen=True, slots=True)
class FakeVisionRequestLog:
    source_host: str
    source_port: int
    inspection_request_id: str
    inspection_cycle: int
    inspection_mode: str
    slots: tuple[str, ...]


class _FakeVisionProtocol(asyncio.DatagramProtocol):
    def __init__(self, simulator: "FakeVisionIncomingQASimulator") -> None:
        self._simulator = simulator

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._simulator._set_transport(transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._simulator.handle_datagram(data, addr)


class FakeVisionIncomingQASimulator:
    """In-memory idempotent development peer that uses real UDP datagrams."""

    def __init__(self, config: FakeVisionIncomingQAConfig) -> None:
        self._config = config
        self._transport: asyncio.DatagramTransport | None = None
        self._request_context_by_id: dict[str, str] = {}
        self._dropped_first_ack = False
        self.requests: list[FakeVisionRequestLog] = []
        self.ack_destinations: list[tuple[str, int]] = []
        self.result_destinations: list[tuple[str, int]] = []
        self.invalid_datagram_count = 0

    @property
    def bound_port(self) -> int | None:
        if self._transport is None:
            return None
        udp_socket = self._transport.get_extra_info("socket")
        return None if udp_socket is None else int(udp_socket.getsockname()[1])

    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _FakeVisionProtocol(self),
            local_addr=(self._config.host, self._config.request_port),
        )
        self._transport = transport
        logger.info(
            "Fake Vision Incoming QA listening on %s:%s; final results target %s:%s",
            self._config.host,
            self.bound_port,
            self._config.server_result_host,
            self._config.server_result_port,
        )

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def serve_forever(self) -> None:
        await self.start()
        await asyncio.Future[None]()

    def _set_transport(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.DatagramTransport):
            raise RuntimeError("Fake Vision requires a datagram transport.")
        self._transport = transport

    def handle_datagram(self, data: bytes, source: tuple[str, int]) -> None:
        if not self._config.allow_non_loopback and not _is_loopback_host(source[0]):
            logger.warning("[REJECT] non-loopback request source=%s:%s", source[0], source[1])
            self.invalid_datagram_count += 1
            return
        try:
            payload = json.loads(data.decode("utf-8"))
            request = IncomingQARequestV02.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            self.invalid_datagram_count += 1
            logger.warning("[REJECT] invalid incoming_qa_request source=%s:%s error=%s", source[0], source[1], type(exc).__name__)
            return

        self.requests.append(
            FakeVisionRequestLog(
                source_host=source[0],
                source_port=source[1],
                inspection_request_id=request.inspection_request_id,
                inspection_cycle=request.inspection_cycle,
                inspection_mode=request.inspection_mode.value,
                slots=tuple(item.slot_id for item in request.items),
            )
        )
        logger.info(
            "[REQUEST] source=%s:%s request_id=%s mode=%s cycle=%s items=%s",
            source[0], source[1], request.inspection_request_id, request.inspection_mode.value,
            request.inspection_cycle, ",".join(item.slot_id for item in request.items),
        )

        canonical = _canonical_request(request)
        previous = self._request_context_by_id.get(request.inspection_request_id)
        if previous is not None and previous != canonical:
            self._send_ack(request, source, accepted=False, duplicate=False, reason_code="CONTRACT_CONFLICT")
            return
        duplicate = previous is not None
        self._request_context_by_id[request.inspection_request_id] = canonical

        if self._config.drop_first_ack and not self._dropped_first_ack:
            self._dropped_first_ack = True
            # Suppressing the paired result is intentional: otherwise the FMS
            # would complete the transaction before the retry path is exercised.
            logger.info("[ACK] intentionally dropped destination=%s:%s", source[0], source[1])
            return

        self._send_ack(request, source, accepted=True, duplicate=duplicate, reason_code=None)
        if self._config.result_delay_seconds:
            asyncio.get_running_loop().call_later(self._config.result_delay_seconds, self._send_result, request)
        else:
            self._send_result(request)

    def _send_ack(
        self,
        request: IncomingQARequestV02,
        destination: tuple[str, int],
        *,
        accepted: bool,
        duplicate: bool,
        reason_code: str | None,
    ) -> None:
        self._require_transport()
        payload: dict[str, Any] = {
            "inspection_request_id": request.inspection_request_id,
            "inspection_cycle": request.inspection_cycle,
            "accepted": accepted,
            "duplicate": duplicate,
        }
        if reason_code is not None:
            payload["reason_code"] = reason_code
        ack = IncomingQAAckV02.model_validate(payload)
        encoded = ack.model_dump_json().encode("utf-8")
        self._send_to(encoded, destination)
        self.ack_destinations.append(destination)
        logger.info("[ACK] destination=%s:%s accepted=%s duplicate=%s", destination[0], destination[1], accepted, duplicate)
        if accepted and self._config.duplicate_ack:
            self._send_to(encoded, destination)
            self.ack_destinations.append(destination)
            logger.info("[ACK] duplicate destination=%s:%s", destination[0], destination[1])

    def _send_result(self, request: IncomingQARequestV02) -> None:
        self._require_transport()
        result = _scenario_result(request, self._config.scenario)
        destination = (self._config.server_result_host, self._config.server_result_port)
        encoded = result.model_dump_json().encode("utf-8")
        self._send_to(encoded, destination)
        self.result_destinations.append(destination)
        logger.info("[RESULT] destination=%s:%s result=%s", destination[0], destination[1], result.result)
        if self._config.duplicate_result:
            self._send_to(encoded, destination)
            self.result_destinations.append(destination)
            logger.info("[RESULT] duplicate destination=%s:%s", destination[0], destination[1])

    def _send_to(self, payload: bytes, destination: tuple[str, int]) -> None:
        if not self._config.allow_non_loopback and not _is_loopback_host(destination[0]):
            raise FakeVisionConfigurationError("Refusing non-loopback UDP send without explicit opt-in.")
        self._require_transport().sendto(payload, destination)

    def _require_transport(self) -> asyncio.DatagramTransport:
        if self._transport is None:
            raise RuntimeError("Fake Vision simulator has not been started.")
        return self._transport


def _canonical_request(request: IncomingQARequestV02) -> str:
    payload = request.model_dump(mode="json")
    payload["items"] = sorted(payload["items"], key=lambda item: (item["delivery_item_id"], item["slot_id"]))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _scenario_result(request: IncomingQARequestV02, scenario: FakeVisionScenario) -> IncomingQAResultV02:
    overall = "PASS"
    production_valid = True
    failing_slots: set[str] = set()
    not_evaluated_slots: set[str] = set()
    if request.inspection_mode is IncomingQAInspectionMode.BASE_AB:
        if scenario is FakeVisionScenario.BASE_FAIL:
            overall, production_valid, failing_slots = "FAIL", False, {item.slot_id for item in request.items}
        elif scenario is FakeVisionScenario.BASE_NOT_EVALUATED:
            overall, production_valid, not_evaluated_slots = "NOT_EVALUATED", False, {item.slot_id for item in request.items}
    elif request.inspection_mode is IncomingQAInspectionMode.HOUSE_B and scenario is FakeVisionScenario.B02_FAIL:
        overall, production_valid, failing_slots = "FAIL", False, {"B02"}

    items = [
        _result_item(item, failure=item.slot_id in failing_slots, not_evaluated=item.slot_id in not_evaluated_slots)
        for item in request.items
    ]
    return IncomingQAResultV02(
        inspection_request_id=request.inspection_request_id,
        inspection_cycle=request.inspection_cycle,
        inspection_mode=request.inspection_mode,
        result=overall,
        production_valid=production_valid,
        items=items,
        camera_source="GLOBAL_CAMERA",
        timestamp=datetime.now(UTC),
        model_scope="fake-vision-incoming-qa",
        model_version="v0.2-local-fake",
    )


def _result_item(
    item: IncomingQARequestItemV02,
    *,
    failure: bool,
    not_evaluated: bool,
) -> IncomingQAResultItemV02:
    if failure:
        return IncomingQAResultItemV02(
            **item.model_dump(), predicted_class_name=item.expected_class_name,
            material_confidence=0.20, detected_quantity=0, result="FAIL",
            failure_type="DEFECT", defects=["COLOR_NG"], quality_scores={"surface": 0.20},
        )
    if not_evaluated:
        return IncomingQAResultItemV02(
            **item.model_dump(), predicted_class_name=None,
            material_confidence=None, detected_quantity=0, result="NOT_EVALUATED",
            failure_type=None, defects=[], quality_scores=None,
        )
    return IncomingQAResultItemV02(
        **item.model_dump(), predicted_class_name=item.expected_class_name,
        material_confidence=0.99, detected_quantity=item.expected_quantity, result="PASS",
        failure_type=None, defects=[], quality_scores={"surface": 0.99},
    )


def _environment_port(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("FAKE_VISION_HOST", "127.0.0.1"))
    parser.add_argument("--request-port", type=int, default=_environment_port("FAKE_VISION_REQUEST_PORT"))
    parser.add_argument("--server-result-host", default=os.getenv("SERVER_RESULT_HOST") or os.getenv("FMS_INCOMING_QA_RESULT_UDP_HOST"))
    parser.add_argument("--server-result-port", type=int, default=_environment_port("SERVER_RESULT_PORT") or _environment_port("FMS_INCOMING_QA_RESULT_UDP_PORT"))
    parser.add_argument("--scenario", choices=[item.value for item in FakeVisionScenario], default=FakeVisionScenario.ALL_PASS.value)
    parser.add_argument("--drop-first-ack", action="store_true")
    parser.add_argument("--duplicate-ack", action="store_true")
    parser.add_argument("--duplicate-result", action="store_true")
    parser.add_argument("--result-delay-ms", type=float, default=0.0, help="Local simulator delay before final result; default 0.")
    parser.add_argument("--allow-non-loopback", action="store_true")
    args = parser.parse_args(argv)
    if args.request_port is None or args.server_result_host is None or args.server_result_port is None:
        parser.error("--request-port, --server-result-host, and --server-result-port are required (no port defaults).")
    return args


async def _main_async(args: argparse.Namespace) -> None:
    simulator = FakeVisionIncomingQASimulator(
        FakeVisionIncomingQAConfig(
            host=args.host,
            request_port=args.request_port,
            server_result_host=args.server_result_host,
            server_result_port=args.server_result_port,
            scenario=FakeVisionScenario(args.scenario),
            allow_non_loopback=args.allow_non_loopback,
            drop_first_ack=args.drop_first_ack,
            duplicate_ack=args.duplicate_ack,
            duplicate_result=args.duplicate_result,
            result_delay_seconds=args.result_delay_ms / 1000,
        )
    )
    try:
        await simulator.serve_forever()
    finally:
        await simulator.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        args = _parse_args(argv)
        asyncio.run(_main_async(args))
    except FakeVisionConfigurationError as exc:
        print(f"Fake Vision configuration rejected: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
