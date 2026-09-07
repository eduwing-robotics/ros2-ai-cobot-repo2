#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path

import argparse
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request


# ============================================================
# LOCAL IMPORT
# ============================================================


SCRIPT_DIR = Path(
    __file__
).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(SCRIPT_DIR),
    )


from contracts_v1 import (
    RESULT_MESSAGE_TYPE,
    canonical_json_bytes,
    result_fingerprint,
)

from udp_gateway_v2 import (
    GatewayStore,
    MAX_DATAGRAM,
    compact_json_bytes,
    now_utc,
    process_datagram,
    submit_controller_http,
)


# ============================================================
# HARMONY PRE_ROOF UDP GATEWAY V3
#
# IMPLEMENTED
# - Request UDP :20061
# - Contract validation
# - Controller V3 acceptance before accepted=true ACK
# - Duplicate / REQUEST_CONFLICT
# - SQLite request transaction
# - Final Result watcher
# - Final Result UDP TX
# - Idempotent local result-send persistence
#
# Controller:
#   http://127.0.0.1:8814
#
# Default Final Result:
#   192.168.20.20:20062/UDP
#
# Actual Team Server E2E is NOT yet claimed.
# ============================================================


DEFAULT_BIND_HOST = "192.168.20.30"
DEFAULT_REQUEST_PORT = 20061

DEFAULT_CONTROLLER_URL = (
    "http://127.0.0.1:8814"
    "/api/server-request"
)

DEFAULT_CONTROLLER_FINAL_URL = (
    "http://127.0.0.1:8814"
    "/api/final-result"
)

DEFAULT_RESULT_HOST = "192.168.20.20"
DEFAULT_RESULT_PORT = 20062


ROOT = (
    Path.home() /
    "vision_project"
)

STATE_DIR = (
    ROOT /
    "datasets/harmony_parts_v1/"
    "pre_roof_qc/integration/"
    "udp_gateway_v3"
)

DEFAULT_DB = (
    STATE_DIR /
    "transactions.sqlite3"
)


# ============================================================
# RESULT STORE
# ============================================================


class ResultStore:

    def __init__(
        self,
        path,
    ):

        self.path = Path(
            path
        )

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.lock = threading.RLock()

        self.conn = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )

        self.conn.row_factory = sqlite3.Row

        self.conn.execute(
            "PRAGMA journal_mode=WAL"
        )

        self.conn.execute(
            "PRAGMA synchronous=FULL"
        )

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS result_transmissions (
                inspection_request_id TEXT NOT NULL,
                inspection_cycle INTEGER NOT NULL,

                result_fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL,

                result_host TEXT NOT NULL,
                result_port INTEGER NOT NULL,

                transmission_status TEXT NOT NULL,

                first_seen_at TEXT NOT NULL,
                last_attempt_at TEXT,
                sent_at TEXT,

                send_count INTEGER NOT NULL,

                PRIMARY KEY (
                    inspection_request_id,
                    inspection_cycle
                )
            )
            """
        )

    def close(self):

        with self.lock:
            self.conn.close()

    def get(
        self,
        request_id,
        cycle,
    ):

        with self.lock:

            return self.conn.execute(
                """
                SELECT *
                FROM result_transmissions
                WHERE
                    inspection_request_id = ?
                    AND inspection_cycle = ?
                """,
                (
                    request_id,
                    cycle,
                ),
            ).fetchone()

    def prepare(
        self,
        *,
        payload,
        result_host,
        result_port,
    ):

        request_id = payload[
            "inspection_request_id"
        ]

        cycle = int(
            payload[
                "inspection_cycle"
            ]
        )

        fingerprint = (
            result_fingerprint(
                payload
            )
        )

        raw_json = (
            canonical_json_bytes(
                payload
            ).decode(
                "utf-8"
            )
        )

        with self.lock:

            row = self.get(
                request_id,
                cycle,
            )

            if row is None:

                self.conn.execute(
                    """
                    INSERT INTO result_transmissions (
                        inspection_request_id,
                        inspection_cycle,

                        result_fingerprint,
                        result_json,

                        result_host,
                        result_port,

                        transmission_status,

                        first_seen_at,
                        last_attempt_at,
                        sent_at,

                        send_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        cycle,

                        fingerprint,
                        raw_json,

                        result_host,
                        int(result_port),

                        "PENDING",

                        now_utc(),
                        None,
                        None,

                        0,
                    ),
                )

                return {
                    "action":
                        "SEND",

                    "fingerprint":
                        fingerprint,
                }

            if (
                row[
                    "result_fingerprint"
                ]
                !=
                fingerprint
            ):

                return {
                    "action":
                        "CONFLICT",

                    "stored_fingerprint":
                        row[
                            "result_fingerprint"
                        ],

                    "incoming_fingerprint":
                        fingerprint,
                }

            if (
                row[
                    "transmission_status"
                ]
                ==
                "SENT"
            ):

                return {
                    "action":
                        "ALREADY_SENT",

                    "fingerprint":
                        fingerprint,
                }

            return {
                "action":
                    "SEND",

                "fingerprint":
                    fingerprint,
            }

    def mark_attempt(
        self,
        *,
        request_id,
        cycle,
    ):

        with self.lock:

            row = self.get(
                request_id,
                cycle,
            )

            if row is None:
                raise RuntimeError(
                    "result row missing"
                )

            count = (
                int(
                    row[
                        "send_count"
                    ]
                )
                + 1
            )

            self.conn.execute(
                """
                UPDATE result_transmissions
                SET
                    last_attempt_at = ?,
                    send_count = ?
                WHERE
                    inspection_request_id = ?
                    AND inspection_cycle = ?
                """,
                (
                    now_utc(),
                    count,
                    request_id,
                    cycle,
                ),
            )

            return count

    def mark_sent(
        self,
        *,
        request_id,
        cycle,
    ):

        with self.lock:

            self.conn.execute(
                """
                UPDATE result_transmissions
                SET
                    transmission_status = 'SENT',
                    sent_at = ?
                WHERE
                    inspection_request_id = ?
                    AND inspection_cycle = ?
                """,
                (
                    now_utc(),
                    request_id,
                    cycle,
                ),
            )


# ============================================================
# CONTROLLER FINAL RESULT
# ============================================================


def fetch_final_result(
    url,
    timeout=0.75,
):

    try:

        with urllib.request.urlopen(
            url,
            timeout=timeout,
        ) as response:

            payload = json.load(
                response
            )

    except Exception:

        return {
            "online":
                False,
        }


    return {
        "online":
            True,

        "ready":
            bool(
                payload.get(
                    "ready",
                    False,
                )
            ),

        "final_result":
            payload.get(
                "final_result"
            ),
    }


# ============================================================
# RESULT TX ONCE
# ============================================================


def send_final_result_once(
    *,
    payload,
    result_store,
    result_host,
    result_port,
    sock=None,
):

    if not isinstance(
        payload,
        dict,
    ):
        return {
            "outcome":
                "INVALID_RESULT",
        }


    if (
        payload.get(
            "message_type"
        )
        !=
        RESULT_MESSAGE_TYPE
    ):

        return {
            "outcome":
                "INVALID_RESULT_TYPE",
        }


    request_id = payload.get(
        "inspection_request_id"
    )

    cycle = payload.get(
        "inspection_cycle"
    )


    decision = (
        result_store.prepare(
            payload=payload,
            result_host=result_host,
            result_port=result_port,
        )
    )


    if (
        decision[
            "action"
        ]
        ==
        "ALREADY_SENT"
    ):

        return {
            "outcome":
                "ALREADY_SENT",

            "inspection_request_id":
                request_id,

            "inspection_cycle":
                cycle,
        }


    if (
        decision[
            "action"
        ]
        ==
        "CONFLICT"
    ):

        return {
            "outcome":
                "RESULT_CONFLICT_LOCAL",

            "inspection_request_id":
                request_id,

            "inspection_cycle":
                cycle,
        }


    own_socket = False

    if sock is None:

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        own_socket = True


    try:

        result_store.mark_attempt(
            request_id=request_id,
            cycle=cycle,
        )


        sock.sendto(
            compact_json_bytes(
                payload
            ),
            (
                result_host,
                int(result_port),
            ),
        )


        result_store.mark_sent(
            request_id=request_id,
            cycle=cycle,
        )


        return {
            "outcome":
                "RESULT_SENT",

            "inspection_request_id":
                request_id,

            "inspection_cycle":
                cycle,

            "result_host":
                result_host,

            "result_port":
                int(result_port),
        }


    finally:

        if own_socket:
            sock.close()


# ============================================================
# RESULT WATCHER
# ============================================================


def result_watcher(
    *,
    stop_event,
    final_url,
    result_store,
    result_host,
    result_port,
):

    while not stop_event.is_set():

        current = fetch_final_result(
            final_url
        )


        if (
            current.get(
                "online"
            )
            and
            current.get(
                "ready"
            )
            and
            isinstance(
                current.get(
                    "final_result"
                ),
                dict,
            )
        ):

            result = send_final_result_once(
                payload=(
                    current[
                        "final_result"
                    ]
                ),
                result_store=(
                    result_store
                ),
                result_host=(
                    result_host
                ),
                result_port=(
                    result_port
                ),
            )


            if (
                result[
                    "outcome"
                ]
                ==
                "RESULT_SENT"
            ):

                print(
                    f"{now_utc()} "
                    f"OUTCOME=RESULT_SENT "
                    f"REQUEST_ID="
                    f"{result['inspection_request_id']} "
                    f"CYCLE="
                    f"{result['inspection_cycle']} "
                    f"TARGET="
                    f"{result_host}:{result_port}"
                )


            elif (
                result[
                    "outcome"
                ]
                ==
                "RESULT_CONFLICT_LOCAL"
            ):

                print(
                    f"{now_utc()} "
                    f"OUTCOME=RESULT_CONFLICT_LOCAL "
                    f"REQUEST_ID="
                    f"{result['inspection_request_id']} "
                    f"CYCLE="
                    f"{result['inspection_cycle']}"
                )


        stop_event.wait(
            0.25
        )


# ============================================================
# SELF TEST
# ============================================================


class CaptureSocket:

    def __init__(self):
        self.sent = []

    def sendto(
        self,
        raw,
        address,
    ):

        self.sent.append(
            (
                raw,
                address,
            )
        )

        return len(raw)


def run_self_test():

    with tempfile.TemporaryDirectory() as td:

        db = (
            Path(td) /
            "gateway_v3.sqlite3"
        )

        result_store = (
            ResultStore(
                db
            )
        )

        capture = (
            CaptureSocket()
        )


        payload = {
            "ver":
                "0.1",

            "message_type":
                "pre_roof_inspection_result",

            "inspection_request_id":
                "550e8400-e29b-41d4-a716-446655440000",

            "inspection_cycle":
                1,

            "job_id":
                123,

            "inspection_type":
                "PRE_ROOF",

            "status":
                "COMPLETED",

            "overall_result":
                "PASS",

            "vision_production_valid":
                False,

            "views": [
                {
                    "view_name":
                        view,

                    "result":
                        "PASS",

                    "reason_code":
                        None,

                    "defects":
                        [],

                    "metrics":
                        {},
                }
                for view in (
                    "TOP",
                    "LEFT",
                    "RIGHT",
                    "FRONT",
                    "BEHIND",
                )
            ],

            "timestamp":
                "2026-09-05T09:20:00.000Z",

            "runtime_profile":
                "PRE_ROOF_5VIEW",

            "runtime_versions": {
                "controller":
                    "V3",

                "TOP":
                    "V5",

                "LEFT":
                    "V3",

                "RIGHT":
                    "V7",

                "FRONT":
                    "V1",

                "BEHIND":
                    "V4",
            },
        }


        first = (
            send_final_result_once(
                payload=payload,
                result_store=result_store,
                result_host="127.0.0.1",
                result_port=20062,
                sock=capture,
            )
        )


        assert (
            first[
                "outcome"
            ]
            ==
            "RESULT_SENT"
        )

        assert len(
            capture.sent
        ) == 1


        print(
            "TEST 1 FINAL RESULT SEND      : PASS"
        )


        second = (
            send_final_result_once(
                payload=payload,
                result_store=result_store,
                result_host="127.0.0.1",
                result_port=20062,
                sock=capture,
            )
        )


        assert (
            second[
                "outcome"
            ]
            ==
            "ALREADY_SENT"
        )

        assert len(
            capture.sent
        ) == 1


        print(
            "TEST 2 RESULT IDEMPOTENCY     : PASS"
        )


        changed = json.loads(
            json.dumps(
                payload
            )
        )

        changed[
            "overall_result"
        ] = "FAIL"


        third = (
            send_final_result_once(
                payload=changed,
                result_store=result_store,
                result_host="127.0.0.1",
                result_port=20062,
                sock=capture,
            )
        )


        assert (
            third[
                "outcome"
            ]
            ==
            "RESULT_CONFLICT_LOCAL"
        )

        assert len(
            capture.sent
        ) == 1


        print(
            "TEST 3 RESULT CONFLICT BLOCK  : PASS"
        )


        row = result_store.get(
            payload[
                "inspection_request_id"
            ],
            1,
        )


        assert (
            row[
                "transmission_status"
            ]
            ==
            "SENT"
        )

        assert (
            int(
                row[
                    "send_count"
                ]
            )
            ==
            1
        )


        print(
            "TEST 4 RESULT SQLITE          : PASS"
        )


        result_store.close()


    print()
    print(
        "============================================"
    )

    print(
        " PRE_ROOF UDP GATEWAY V3 SELF TEST PASS"
    )

    print(
        "============================================"
    )

    print(
        "Request Controller : "
        "http://127.0.0.1:8814/api/server-request"
    )

    print(
        "Final Result Source: "
        "http://127.0.0.1:8814/api/final-result"
    )

    print(
        "Request UDP        : "
        "192.168.20.30:20061"
    )

    print(
        "Result UDP Default : "
        "192.168.20.20:20062"
    )

    print(
        "Result Idempotency : IMPLEMENTED"
    )

    print(
        "Actual Server E2E  : NOT YET"
    )

    print(
        "vision_production_valid=false"
    )


# ============================================================
# LIVE
# ============================================================


def run_gateway(
    *,
    bind_host,
    request_port,
    db_path,
    controller_url,
    controller_final_url,
    result_host,
    result_port,
):

    store = GatewayStore(
        db_path
    )

    result_store = ResultStore(
        db_path
    )


    request_sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )

    request_sock.bind(
        (
            bind_host,
            request_port,
        )
    )


    def controller_submit(
        payload,
    ):

        return submit_controller_http(
            payload,
            controller_url,
        )


    stop_event = (
        threading.Event()
    )


    watcher = threading.Thread(
        target=result_watcher,
        kwargs={
            "stop_event":
                stop_event,

            "final_url":
                controller_final_url,

            "result_store":
                result_store,

            "result_host":
                result_host,

            "result_port":
                result_port,
        },
        daemon=True,
        name=(
            "pre-roof-result-watcher"
        ),
    )

    watcher.start()


    print()
    print(
        "============================================================"
    )

    print(
        " HARMONY PRE_ROOF UDP GATEWAY V3"
    )

    print(
        "============================================================"
    )

    print(
        f"REQUEST LISTEN : "
        f"{bind_host}:{request_port}/UDP"
    )

    print(
        f"CONTROLLER     : "
        f"{controller_url}"
    )

    print(
        "ACK POLICY     : "
        "CONTROLLER ACCEPT BEFORE accepted=true"
    )

    print(
        "ACK TARGET     : "
        "REQUEST SOURCE IP/PORT"
    )

    print(
        f"FINAL WATCH    : "
        f"{controller_final_url}"
    )

    print(
        f"RESULT TARGET  : "
        f"{result_host}:{result_port}/UDP"
    )

    print(
        "RESULT TX      : IMPLEMENTED"
    )

    print(
        "RESULT POLICY  : "
        "SEND ONCE / LOCAL IDEMPOTENCY"
    )

    print(
        f"SQLITE         : "
        f"{db_path}"
    )

    print(
        "Actual Server E2E : NOT YET"
    )

    print(
        "vision_production_valid=false"
    )

    print(
        "============================================================"
    )


    try:

        while True:

            raw, source = (
                request_sock.recvfrom(
                    MAX_DATAGRAM
                )
            )


            try:

                result = process_datagram(
                    raw=raw,
                    source_addr=source,
                    store=store,
                    controller_submit=(
                        controller_submit
                    ),
                )


                ack = result.get(
                    "ack"
                )


                if ack is not None:

                    request_sock.sendto(
                        compact_json_bytes(
                            ack
                        ),
                        source,
                    )


                request = result.get(
                    "request"
                )

                request_id = (
                    request.get(
                        "inspection_request_id"
                    )
                    if request
                    else "-"
                )

                cycle = (
                    request.get(
                        "inspection_cycle"
                    )
                    if request
                    else "-"
                )


                print(
                    f"{now_utc()} "
                    f"SOURCE="
                    f"{source[0]}:{source[1]} "
                    f"OUTCOME="
                    f"{result['outcome']} "
                    f"REQUEST_ID="
                    f"{request_id} "
                    f"CYCLE="
                    f"{cycle} "
                    f"ACK="
                    f"{'YES' if ack else 'NO'}"
                )


            except Exception as exc:

                print(
                    f"{now_utc()} "
                    f"SOURCE="
                    f"{source[0]}:{source[1]} "
                    f"OUTCOME=INTERNAL_ERROR "
                    f"ERROR="
                    f"{type(exc).__name__}"
                )


    except KeyboardInterrupt:

        print()
        print(
            "Gateway V3 stopped by operator."
        )


    finally:

        stop_event.set()

        watcher.join(
            timeout=2.0
        )

        request_sock.close()

        store.close()

        result_store.close()


# ============================================================
# MAIN
# ============================================================


def main():

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--self-test",
        action="store_true",
    )


    parser.add_argument(
        "--bind-host",
        default=(
            DEFAULT_BIND_HOST
        ),
    )


    parser.add_argument(
        "--request-port",
        type=int,
        default=(
            DEFAULT_REQUEST_PORT
        ),
    )


    parser.add_argument(
        "--controller-url",
        default=(
            DEFAULT_CONTROLLER_URL
        ),
    )


    parser.add_argument(
        "--controller-final-url",
        default=(
            DEFAULT_CONTROLLER_FINAL_URL
        ),
    )


    parser.add_argument(
        "--result-host",
        default=(
            DEFAULT_RESULT_HOST
        ),
    )


    parser.add_argument(
        "--result-port",
        type=int,
        default=(
            DEFAULT_RESULT_PORT
        ),
    )


    parser.add_argument(
        "--db",
        default=str(
            DEFAULT_DB
        ),
    )


    args = parser.parse_args()


    if args.self_test:

        run_self_test()
        return


    run_gateway(
        bind_host=(
            args.bind_host
        ),
        request_port=(
            args.request_port
        ),
        db_path=(
            Path(
                args.db
            )
        ),
        controller_url=(
            args.controller_url
        ),
        controller_final_url=(
            args.controller_final_url
        ),
        result_host=(
            args.result_host
        ),
        result_port=(
            args.result_port
        ),
    )


if __name__ == "__main__":
    main()
