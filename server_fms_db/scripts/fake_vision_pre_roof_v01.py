"""Loopback-only Fake Vision for PRE_ROOF Wire Contract v0.1.

Example (development only):
``python -m scripts.fake_vision_pre_roof_v01 --request-port 31061 --result-port 31062``
It sends ACKs to each request's source port and Final Results to the separately
configured server result port, matching the real contract without contacting a
Vision PC by default.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import logging
from typing import Any

from shared.schemas.pre_roof_vision import PreRoofInspectionRequestV01

VIEWS = ("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")
VERSIONS = {"controller": "V3", "TOP": "V5", "LEFT": "V3", "RIGHT": "V7", "FRONT": "V1", "BEHIND": "V4"}
SCENARIOS = ("all-pass-valid-false", "all-pass-valid-true", "fail", "not-evaluated-runtime", "not-evaluated-error", "ack-drop", "duplicate-ack", "duplicate-result", "result-conflict", "request-conflict", "delayed-result")


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class FakePreRoofVision(asyncio.DatagramProtocol):
    def __init__(self, *, result_host: str, result_port: int, scenario: str, delay: float) -> None:
        self.result_host, self.result_port, self.scenario, self.delay = result_host, result_port, scenario, delay
        self.transport: asyncio.DatagramTransport | None = None
        self._requests: dict[tuple[str, int], str] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.create_task(self._handle(data, addr))

    async def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            request = PreRoofInspectionRequestV01.model_validate_json(data)
        except Exception as exc:
            logging.warning("[INVALID REQUEST] source=%s error=%s", addr, exc)
            return
        assert self.transport is not None
        payload = request.model_dump(mode="json")
        fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = (str(request.inspection_request_id), request.inspection_cycle)
        previous = self._requests.get(key)
        conflict = self.scenario == "request-conflict" or (previous is not None and previous != fingerprint)
        duplicate = previous is not None and not conflict
        self._requests[key] = fingerprint
        logging.info("[REQUEST] source=%s:%s request_id=%s cycle=%s", addr[0], addr[1], request.inspection_request_id, request.inspection_cycle)
        if self.scenario != "ack-drop":
            ack = {"ver":"0.1", "message_type":"pre_roof_inspection_ack", "inspection_request_id":str(request.inspection_request_id), "inspection_cycle":request.inspection_cycle, "accepted":not conflict, "duplicate":duplicate if not conflict else False, "reason_code":"REQUEST_CONFLICT" if conflict else None}
            self.transport.sendto(json.dumps(ack).encode(), addr)
            logging.info("[ACK] destination=%s:%s accepted=%s duplicate=%s", addr[0], addr[1], not conflict, duplicate)
            if self.scenario == "duplicate-ack":
                self.transport.sendto(json.dumps({**ack, "duplicate": True}).encode(), addr)
        if conflict or self.scenario == "ack-drop":
            return
        if self.scenario == "delayed-result":
            await asyncio.sleep(self.delay)
        result = self._result(request)
        self.transport.sendto(json.dumps(result).encode(), (self.result_host, self.result_port))
        logging.info("[RESULT] destination=%s:%s result=%s", self.result_host, self.result_port, result["overall_result"])
        if self.scenario == "duplicate-result":
            self.transport.sendto(json.dumps(result).encode(), (self.result_host, self.result_port))
        if self.scenario == "result-conflict":
            conflicting = {**result, "overall_result":"FAIL", "vision_production_valid":False,
                "views":[{**view, "result":"FAIL"} for view in result["views"]]}
            self.transport.sendto(json.dumps(conflicting).encode(), (self.result_host, self.result_port))

    def _result(self, request: PreRoofInspectionRequestV01) -> dict[str, Any]:
        outcome, valid, reason = "PASS", self.scenario == "all-pass-valid-true", None
        if self.scenario == "fail": outcome = "FAIL"
        elif self.scenario == "not-evaluated-runtime": outcome, reason = "NOT_EVALUATED", "RUNTIME_NOT_EVALUATED"
        elif self.scenario == "not-evaluated-error": outcome, reason = "NOT_EVALUATED", "RUNTIME_ERROR"
        views = [{"view_name": view, "result":outcome, "reason_code":reason, "defects":[], "metrics":{}} for view in VIEWS]
        return {"ver":"0.1", "message_type":"pre_roof_inspection_result", "inspection_request_id":str(request.inspection_request_id), "inspection_cycle":request.inspection_cycle, "job_id":request.job_id, "inspection_type":"PRE_ROOF", "status":"COMPLETED", "overall_result":outcome, "vision_production_valid":valid, "views":views, "timestamp":datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "runtime_profile":"PRE_ROOF_5VIEW", "runtime_versions":VERSIONS}


async def run(args: argparse.Namespace) -> None:
    if not args.allow_non_loopback and (not _loopback(args.host) or not _loopback(args.result_host)):
        raise SystemExit("Refusing non-loopback bind/send. Use --allow-non-loopback only after deliberate review.")
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: FakePreRoofVision(result_host=args.result_host, result_port=args.result_port, scenario=args.scenario, delay=args.delay),
        local_addr=(args.host, args.request_port),
    )
    logging.info("Fake PRE_ROOF Vision listening on %s:%s scenario=%s", args.host, args.request_port, args.scenario)
    try:
        await asyncio.Future()
    finally:
        transport.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--request-port", type=int, required=True)
    parser.add_argument("--result-host", default="127.0.0.1")
    parser.add_argument("--result-port", type=int, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="all-pass-valid-true")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--allow-non-loopback", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
