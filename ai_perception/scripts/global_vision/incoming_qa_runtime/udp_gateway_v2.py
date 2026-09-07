#!/usr/bin/env python3
"""Harmony Incoming QA v0.2 UDP Gateway.

Server UDP Request
  -> validation/idempotency
  -> ROS2 /vision/incoming_qa/request_v02

ROS2 /vision/incoming_qa/result_v02
  -> validation
  -> Server UDP Final Result

No production authorization is granted by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

import rclpy

from pydantic import ValidationError
from rclpy.node import Node
from std_msgs.msg import String

from contracts_v2 import (
    IncomingQaRequestV02,
    IncomingQaResultV02,
)
from transaction_store_v2 import (
    TransactionStoreV2,
)


ROOT = Path.home() / "vision_project"

REQUEST_TOPIC = (
    "/vision/incoming_qa/request_v02"
)
RESULT_TOPIC = (
    "/vision/incoming_qa/result_v02"
)

LISTEN_HOST = os.environ.get(
    "HARMONY_INCOMING_QA_LISTEN_HOST",
    "0.0.0.0",
)

REQUEST_PORT_RAW = os.environ.get(
    "HARMONY_INCOMING_QA_REQUEST_PORT"
)

SERVER_HOST = os.environ.get(
    "HARMONY_INCOMING_QA_SERVER_HOST"
)

SERVER_PORT_RAW = os.environ.get(
    "HARMONY_INCOMING_QA_SERVER_PORT"
)

DB_PATH = Path(
    os.environ.get(
        "HARMONY_INCOMING_QA_DB_V2",
        str(
            ROOT
            / "datasets/harmony_house_vision_v1/runtime/"
              "incoming_qa/"
              "incoming_qa_transactions_v2.sqlite3"
        ),
    )
)


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat().replace(
        "+00:00",
        "Z",
    )


def sha256_json(payload):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(
        body
    ).hexdigest()


def require_port(
    raw,
    name,
):
    if raw is None:
        raise SystemExit(
            f"CONFIG_REQUIRED={name}"
        )

    value = int(raw)

    if not (1 <= value <= 65535):
        raise SystemExit(
            f"BAD_PORT={name}:{value}"
        )

    return value


class IncomingQaUdpGatewayV2(Node):
    def __init__(
        self,
        *,
        request_port,
        server_host,
        server_port,
    ):
        super().__init__(
            "harmony_incoming_qa_udp_gateway_v2"
        )

        self.request_port = request_port
        self.server_host = server_host
        self.server_port = server_port

        self.store = TransactionStoreV2(
            DB_PATH
        )

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.bind(
            (
                LISTEN_HOST,
                request_port,
            )
        )

        self.sock.setblocking(
            False
        )

        self.request_pub = (
            self.create_publisher(
                String,
                REQUEST_TOPIC,
                10,
            )
        )

        self.create_subscription(
            String,
            RESULT_TOPIC,
            self.result_callback,
            10,
        )

        self.create_timer(
            0.02,
            self.poll_udp,
        )

        print(
            "============================================================"
        )
        print(
            "HARMONY INCOMING QA UDP GATEWAY V2"
        )
        print(
            "============================================================"
        )
        print(
            f"LISTEN={LISTEN_HOST}:{request_port}"
        )
        print(
            f"RESULT_DEST={server_host}:{server_port}"
        )
        print(
            f"REQUEST_TOPIC={REQUEST_TOPIC}"
        )
        print(
            f"RESULT_TOPIC={RESULT_TOPIC}"
        )
        print(
            f"DB={DB_PATH}"
        )
        print(
            "WIRE_VERSION=0.2"
        )
        print(
            "PRODUCTION_RUNTIME_AUTHORIZED=false"
        )

    def send_json(
        self,
        payload,
        target,
    ):
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.sock.sendto(
            data,
            target,
        )

    def send_ack(
        self,
        *,
        request,
        source,
        accepted,
        duplicate,
        outcome,
    ):
        payload = {
            "ver": "0.2",
            "message_type":
                "incoming_qa_ack",
            "inspection_request_id":
                request.get(
                    "inspection_request_id"
                ),
            "inspection_cycle":
                request.get(
                    "inspection_cycle"
                ),
            "accepted":
                accepted,
            "duplicate":
                duplicate,
        }

        self.send_json(
            payload,
            source,
        )

    def poll_udp(self):
        while True:
            try:
                data, source = (
                    self.sock.recvfrom(
                        65535
                    )
                )
            except BlockingIOError:
                return

            try:
                raw = json.loads(
                    data.decode(
                        "utf-8"
                    )
                )

                request = (
                    IncomingQaRequestV02
                    .model_validate(raw)
                )

            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                print(
                    f"REQUEST_REJECTED="
                    f"BAD_REQUEST:{exc}",
                    flush=True,
                )
                continue

            payload = request.model_dump(
                mode="json"
            )

            outcome = self.store.register(
                request_id=(
                    request.inspection_request_id
                ),
                inspection_cycle=(
                    request.inspection_cycle
                ),
                inspection_mode=(
                    request.inspection_mode.value
                ),
                payload=payload,
                payload_sha256=(
                    sha256_json(payload)
                ),
                accepted_at=utc_now(),
                reply_host=source[0],
                reply_port=source[1],
            )

            if outcome == "CONFLICT":
                self.send_ack(
                    request=payload,
                    source=source,
                    accepted=False,
                    duplicate=False,
                    outcome="CONTRACT_CONFLICT",
                )
                continue

            if outcome == "CYCLE_CONFLICT":
                self.send_ack(
                    request=payload,
                    source=source,
                    accepted=False,
                    duplicate=False,
                    outcome="CYCLE_CONFLICT",
                )
                continue

            duplicate = (
                outcome == "DUPLICATE"
            )

            self.send_ack(
                request=payload,
                source=source,
                accepted=True,
                duplicate=duplicate,
                outcome=outcome,
            )

            if duplicate:
                existing = self.store.get(
                    request.inspection_request_id
                )

                if (
                    existing is not None
                    and existing["result_json"]
                ):
                    stored = json.loads(
                        existing["result_json"]
                    )

                    self.send_json(
                        stored,
                        (
                            self.server_host,
                            self.server_port,
                        ),
                    )

                continue

            msg = String()

            msg.data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )

            self.request_pub.publish(
                msg
            )

            self.store.set_status(
                request.inspection_request_id,
                "ACCEPTED",
                utc_now(),
            )

            print(
                "REQUEST_ACCEPTED "
                f"id={request.inspection_request_id} "
                f"cycle={request.inspection_cycle} "
                f"mode={request.inspection_mode.value} "
                f"items={len(request.items)}",
                flush=True,
            )

    def result_callback(
        self,
        msg,
    ):
        try:
            raw = json.loads(
                msg.data
            )

            result = (
                IncomingQaResultV02
                .model_validate(raw)
            )

        except (
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            print(
                f"RESULT_REJECTED={exc}",
                flush=True,
            )
            return

        existing = self.store.get(
            result.inspection_request_id
        )

        if existing is None:
            print(
                "RESULT_REJECTED="
                "UNKNOWN_REQUEST_ID:"
                f"{result.inspection_request_id}",
                flush=True,
            )
            return

        if (
            int(existing["inspection_cycle"])
            != result.inspection_cycle
            or existing["inspection_mode"]
            != result.inspection_mode.value
        ):
            print(
                "RESULT_REJECTED="
                "CORRELATION_MISMATCH:"
                f"{result.inspection_request_id}",
                flush=True,
            )
            return

        payload = result.model_dump(
            mode="json",
            exclude_none=False,
        )

        self.send_json(
            payload,
            (
                self.server_host,
                self.server_port,
            ),
        )

        self.store.set_result(
            result.inspection_request_id,
            payload,
            utc_now(),
        )

        print(
            "FINAL_RESULT_SENT "
            f"id={result.inspection_request_id} "
            f"cycle={result.inspection_cycle} "
            f"mode={result.inspection_mode.value} "
            f"result={result.result}",
            flush=True,
        )

    def destroy_node(self):
        try:
            self.sock.close()
        finally:
            super().destroy_node()


def main():
    request_port = require_port(
        REQUEST_PORT_RAW,
        "HARMONY_INCOMING_QA_REQUEST_PORT",
    )

    if not SERVER_HOST:
        raise SystemExit(
            "CONFIG_REQUIRED="
            "HARMONY_INCOMING_QA_SERVER_HOST"
        )

    server_port = require_port(
        SERVER_PORT_RAW,
        "HARMONY_INCOMING_QA_SERVER_PORT",
    )

    rclpy.init()

    node = IncomingQaUdpGatewayV2(
        request_port=request_port,
        server_host=SERVER_HOST,
        server_port=server_port,
    )

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
