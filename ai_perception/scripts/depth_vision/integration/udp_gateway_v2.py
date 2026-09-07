#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path

import argparse
import json
import socket
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

from datetime import (
    datetime,
    timezone,
)


# ============================================================
# LOCAL CONTRACT IMPORT
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
    ACK_REASON_INVALID_REQUEST,
    ACK_REASON_REQUEST_CONFLICT,
    ContractError,
    build_ack,
    request_fingerprint,
    request_identity,
    validate_positive_int,
    validate_request,
    validate_uuid,
)


# ============================================================
# HARMONY PRE_ROOF UDP GATEWAY V2
#
# V2 responsibility
#
# Server UDP Request
#     -> Contract validation
#     -> Duplicate / Request Conflict check
#     -> Controller V2 transaction acceptance
#     -> Persist accepted transaction
#     -> ACK to UDP request source
#
# IMPORTANT
# ACK accepted=true is produced only after
# Controller V2 accepts the transaction.
#
# NOT YET
# - View inspection
# - Final Result UDP TX
# ============================================================


DEFAULT_BIND_HOST = "192.168.20.30"
DEFAULT_REQUEST_PORT = 20061

DEFAULT_CONTROLLER_URL = (
    "http://127.0.0.1:8813"
    "/api/server-request"
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
    "udp_gateway_v2"
)

DEFAULT_DB = (
    STATE_DIR /
    "transactions.sqlite3"
)

MAX_DATAGRAM = 65535


ACK_REASON_CONTROLLER_BUSY = (
    "CONTROLLER_BUSY"
)

ACK_REASON_CONTROLLER_UNAVAILABLE = (
    "CONTROLLER_UNAVAILABLE"
)

ACK_REASON_INTERNAL_ERROR = (
    "INTERNAL_ERROR"
)


# ============================================================
# UTILITY
# ============================================================


def now_utc():

    return (
        datetime.now(
            timezone.utc
        )
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z",
        )
    )


def compact_json_bytes(
    payload,
):

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )


# ============================================================
# DATABASE
# ============================================================


class GatewayStore:


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

        self.conn = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )

        self.conn.row_factory = (
            sqlite3.Row
        )

        self.conn.execute(
            "PRAGMA journal_mode=WAL"
        )

        self.conn.execute(
            "PRAGMA synchronous=FULL"
        )

        self._init_schema()


    def _init_schema(self):

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                inspection_request_id TEXT NOT NULL,
                inspection_cycle INTEGER NOT NULL,

                request_fingerprint TEXT NOT NULL,
                request_json TEXT NOT NULL,

                job_id INTEGER NOT NULL,
                job_code TEXT NOT NULL,

                source_ip TEXT NOT NULL,
                source_port INTEGER NOT NULL,

                first_received_at TEXT NOT NULL,
                last_received_at TEXT NOT NULL,

                receive_count INTEGER NOT NULL,

                controller_duplicate INTEGER NOT NULL,
                transaction_status TEXT NOT NULL,

                PRIMARY KEY (
                    inspection_request_id,
                    inspection_cycle
                )
            )
            """
        )


        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,

                inspection_request_id TEXT,
                inspection_cycle INTEGER,

                source_ip TEXT,
                source_port INTEGER,

                detail_json TEXT
            )
            """
        )


    def close(self):

        self.conn.close()


    def log_event(
        self,
        *,
        event_type,
        source_addr,
        inspection_request_id=None,
        inspection_cycle=None,
        detail=None,
    ):

        source_ip = None
        source_port = None

        if source_addr is not None:

            source_ip = str(
                source_addr[0]
            )

            source_port = int(
                source_addr[1]
            )


        detail_json = None

        if detail is not None:

            detail_json = json.dumps(
                detail,
                ensure_ascii=False,
                sort_keys=True,
                separators=(
                    ",",
                    ":",
                ),
            )


        self.conn.execute(
            """
            INSERT INTO events (
                timestamp,
                event_type,
                inspection_request_id,
                inspection_cycle,
                source_ip,
                source_port,
                detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc(),
                event_type,
                inspection_request_id,
                inspection_cycle,
                source_ip,
                source_port,
                detail_json,
            ),
        )


    def lookup(
        self,
        request_id,
        cycle,
    ):

        return self.conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE
                inspection_request_id = ?
                AND inspection_cycle = ?
            """,
            (
                request_id,
                cycle,
            ),
        ).fetchone()


    def record_first_accept(
        self,
        *,
        request,
        fingerprint,
        source_addr,
        controller_duplicate,
    ):

        request_id, cycle = (
            request_identity(
                request
            )
        )

        now = now_utc()

        source_ip = str(
            source_addr[0]
        )

        source_port = int(
            source_addr[1]
        )


        self.conn.execute(
            "BEGIN IMMEDIATE"
        )

        try:

            existing = self.lookup(
                request_id,
                cycle,
            )

            if existing is not None:

                self.conn.execute(
                    "ROLLBACK"
                )

                raise RuntimeError(
                    "transaction appeared "
                    "during first accept"
                )


            self.conn.execute(
                """
                INSERT INTO transactions (
                    inspection_request_id,
                    inspection_cycle,

                    request_fingerprint,
                    request_json,

                    job_id,
                    job_code,

                    source_ip,
                    source_port,

                    first_received_at,
                    last_received_at,

                    receive_count,

                    controller_duplicate,
                    transaction_status
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    request_id,
                    cycle,

                    fingerprint,

                    compact_json_bytes(
                        request
                    ).decode(
                        "utf-8"
                    ),

                    request[
                        "job_id"
                    ],

                    request[
                        "job_code"
                    ],

                    source_ip,
                    source_port,

                    now,
                    now,

                    1,

                    (
                        1
                        if controller_duplicate
                        else 0
                    ),

                    "ACCEPTED",
                ),
            )


            self.conn.execute(
                """
                INSERT INTO events (
                    timestamp,
                    event_type,
                    inspection_request_id,
                    inspection_cycle,
                    source_ip,
                    source_port,
                    detail_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    "REQUEST_ACCEPTED",
                    request_id,
                    cycle,
                    source_ip,
                    source_port,

                    json.dumps(
                        {
                            "fingerprint":
                                fingerprint,

                            "controller_duplicate":
                                bool(
                                    controller_duplicate
                                ),
                        },
                        sort_keys=True,
                        separators=(
                            ",",
                            ":",
                        ),
                    ),
                ),
            )


            self.conn.execute(
                "COMMIT"
            )


        except Exception:

            try:
                self.conn.execute(
                    "ROLLBACK"
                )
            except Exception:
                pass

            raise


    def record_duplicate(
        self,
        *,
        request,
        source_addr,
    ):

        request_id, cycle = (
            request_identity(
                request
            )
        )

        now = now_utc()

        row = self.lookup(
            request_id,
            cycle,
        )

        if row is None:

            raise RuntimeError(
                "duplicate row missing"
            )

        new_count = (
            int(
                row[
                    "receive_count"
                ]
            )
            + 1
        )

        source_ip = str(
            source_addr[0]
        )

        source_port = int(
            source_addr[1]
        )


        self.conn.execute(
            """
            UPDATE transactions
            SET
                last_received_at = ?,
                source_ip = ?,
                source_port = ?,
                receive_count = ?
            WHERE
                inspection_request_id = ?
                AND inspection_cycle = ?
            """,
            (
                now,
                source_ip,
                source_port,
                new_count,
                request_id,
                cycle,
            ),
        )


        self.log_event(
            event_type=(
                "REQUEST_DUPLICATE"
            ),
            source_addr=(
                source_addr
            ),
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
            detail={
                "receive_count":
                    new_count,
            },
        )


        return new_count


    def transaction_count(self):

        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM transactions
            """
        ).fetchone()

        return int(
            row[
                "n"
            ]
        )


# ============================================================
# CORRELATION EXTRACTION
# ============================================================


def extract_correlation(
    payload,
):

    if not isinstance(
        payload,
        dict,
    ):
        return None


    try:

        request_id = validate_uuid(
            payload.get(
                "inspection_request_id"
            )
        )

        cycle = validate_positive_int(
            payload.get(
                "inspection_cycle"
            ),
            "inspection_cycle",
        )

    except Exception:

        return None


    return (
        request_id,
        cycle,
    )


# ============================================================
# CONTROLLER
# ============================================================


class ControllerUnavailable(
    RuntimeError
):
    pass


def submit_controller_http(
    request_payload,
    controller_url,
    timeout=1.0,
):

    raw = compact_json_bytes(
        request_payload
    )

    req = urllib.request.Request(
        controller_url,
        data=raw,
        headers={
            "Content-Type":
                "application/json",
        },
        method="POST",
    )


    try:

        with urllib.request.urlopen(
            req,
            timeout=timeout,
        ) as response:

            body = response.read()


    except urllib.error.HTTPError as exc:

        # Controller uses HTTP 409 for legitimate
        # Request Conflict / Busy decisions.

        try:

            body = exc.read()

            result = json.loads(
                body.decode(
                    "utf-8"
                )
            )

        except Exception as parse_exc:

            raise ControllerUnavailable(
                "controller HTTP error "
                "without valid JSON"
            ) from parse_exc


        return result


    except Exception as exc:

        raise ControllerUnavailable(
            type(exc).__name__
        ) from exc


    try:

        return json.loads(
            body.decode(
                "utf-8"
            )
        )

    except Exception as exc:

        raise ControllerUnavailable(
            "controller returned invalid JSON"
        ) from exc


# ============================================================
# DATAGRAM PROCESSING
# ============================================================


def process_datagram(
    *,
    raw,
    source_addr,
    store,
    controller_submit,
):

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    try:

        payload = json.loads(
            raw.decode(
                "utf-8"
            )
        )

    except Exception as exc:

        store.log_event(
            event_type=(
                "INVALID_JSON"
            ),
            source_addr=(
                source_addr
            ),
            detail={
                "error":
                    type(exc).__name__,
            },
        )

        return {
            "outcome":
                "INVALID_JSON_NO_ACK",

            "ack":
                None,
        }


    # --------------------------------------------------------
    # CONTRACT
    # --------------------------------------------------------

    try:

        request = validate_request(
            payload
        )

    except ContractError as exc:

        correlation = (
            extract_correlation(
                payload
            )
        )

        store.log_event(
            event_type=(
                "INVALID_REQUEST"
            ),
            source_addr=(
                source_addr
            ),
            inspection_request_id=(
                correlation[0]
                if correlation
                else None
            ),
            inspection_cycle=(
                correlation[1]
                if correlation
                else None
            ),
            detail={
                "error":
                    str(exc),
            },
        )


        if correlation is None:

            return {
                "outcome":
                    "INVALID_REQUEST_NO_ACK",

                "ack":
                    None,
            }


        ack = build_ack(
            inspection_request_id=(
                correlation[0]
            ),
            inspection_cycle=(
                correlation[1]
            ),
            accepted=False,
            duplicate=False,
            reason_code=(
                ACK_REASON_INVALID_REQUEST
            ),
        )

        return {
            "outcome":
                "INVALID_REQUEST",

            "ack":
                ack,
        }


    request_id, cycle = (
        request_identity(
            request
        )
    )

    fingerprint = (
        request_fingerprint(
            request
        )
    )


    # --------------------------------------------------------
    # EXISTING GATEWAY TRANSACTION
    # --------------------------------------------------------

    row = store.lookup(
        request_id,
        cycle,
    )


    if row is not None:

        if (
            row[
                "request_fingerprint"
            ]
            !=
            fingerprint
        ):

            store.log_event(
                event_type=(
                    "REQUEST_CONFLICT"
                ),
                source_addr=(
                    source_addr
                ),
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
                detail={
                    "stored_fingerprint":
                        row[
                            "request_fingerprint"
                        ],

                    "incoming_fingerprint":
                        fingerprint,
                },
            )

            ack = build_ack(
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
                accepted=False,
                duplicate=False,
                reason_code=(
                    ACK_REASON_REQUEST_CONFLICT
                ),
            )

            return {
                "outcome":
                    "REQUEST_CONFLICT",

                "ack":
                    ack,

                "request":
                    request,
            }


        # Confirm Controller still recognizes
        # the same accepted transaction.

        try:

            controller = (
                controller_submit(
                    request
                )
            )

        except ControllerUnavailable:

            store.log_event(
                event_type=(
                    "CONTROLLER_UNAVAILABLE"
                ),
                source_addr=(
                    source_addr
                ),
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
            )

            ack = build_ack(
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
                accepted=False,
                duplicate=False,
                reason_code=(
                    ACK_REASON_CONTROLLER_UNAVAILABLE
                ),
            )

            return {
                "outcome":
                    "CONTROLLER_UNAVAILABLE",

                "ack":
                    ack,

                "request":
                    request,
            }


        if not controller.get(
            "accepted",
            False,
        ):

            reason = controller.get(
                "reason_code"
            ) or ACK_REASON_INTERNAL_ERROR

            store.log_event(
                event_type=(
                    "CONTROLLER_REJECTED_DUPLICATE"
                ),
                source_addr=(
                    source_addr
                ),
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
                detail={
                    "reason_code":
                        reason,
                },
            )

            ack = build_ack(
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
                accepted=False,
                duplicate=False,
                reason_code=reason,
            )

            return {
                "outcome":
                    "CONTROLLER_REJECTED",

                "ack":
                    ack,

                "request":
                    request,

                "controller":
                    controller,
            }


        count = store.record_duplicate(
            request=request,
            source_addr=source_addr,
        )


        ack = build_ack(
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
            accepted=True,
            duplicate=True,
            reason_code=None,
        )


        return {
            "outcome":
                "DUPLICATE",

            "ack":
                ack,

            "request":
                request,

            "controller":
                controller,

            "receive_count":
                count,
        }


    # --------------------------------------------------------
    # NEW TRANSACTION
    #
    # Controller must accept BEFORE Gateway sends accepted ACK.
    # --------------------------------------------------------

    try:

        controller = controller_submit(
            request
        )

    except ControllerUnavailable:

        store.log_event(
            event_type=(
                "CONTROLLER_UNAVAILABLE"
            ),
            source_addr=(
                source_addr
            ),
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
        )


        ack = build_ack(
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
            accepted=False,
            duplicate=False,
            reason_code=(
                ACK_REASON_CONTROLLER_UNAVAILABLE
            ),
        )


        return {
            "outcome":
                "CONTROLLER_UNAVAILABLE",

            "ack":
                ack,

            "request":
                request,
        }


    if not controller.get(
        "accepted",
        False,
    ):

        reason = controller.get(
            "reason_code"
        ) or ACK_REASON_INTERNAL_ERROR


        store.log_event(
            event_type=(
                "CONTROLLER_REJECTED"
            ),
            source_addr=(
                source_addr
            ),
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
            detail={
                "reason_code":
                    reason,
            },
        )


        ack = build_ack(
            inspection_request_id=(
                request_id
            ),
            inspection_cycle=(
                cycle
            ),
            accepted=False,
            duplicate=False,
            reason_code=reason,
        )


        return {
            "outcome":
                "CONTROLLER_REJECTED",

            "ack":
                ack,

            "request":
                request,

            "controller":
                controller,
        }


    controller_duplicate = bool(
        controller.get(
            "duplicate",
            False,
        )
    )


    store.record_first_accept(
        request=request,
        fingerprint=fingerprint,
        source_addr=source_addr,
        controller_duplicate=(
            controller_duplicate
        ),
    )


    ack = build_ack(
        inspection_request_id=(
            request_id
        ),
        inspection_cycle=(
            cycle
        ),
        accepted=True,
        duplicate=(
            controller_duplicate
        ),
        reason_code=None,
    )


    return {
        "outcome":
            (
                "CONTROLLER_DUPLICATE_RECOVERY"
                if controller_duplicate
                else "ACCEPTED"
            ),

        "ack":
            ack,

        "request":
            request,

        "controller":
            controller,
    }


# ============================================================
# SELF TEST CONTROLLER
# ============================================================


class FakeController:


    def __init__(self):

        self.active = None
        self.fingerprint = None


    def submit(
        self,
        request,
    ):

        fp = request_fingerprint(
            request
        )

        ident = request_identity(
            request
        )


        if self.active is None:

            self.active = request
            self.fingerprint = fp

            return {
                "accepted":
                    True,

                "duplicate":
                    False,

                "reason_code":
                    None,

                "expected_view":
                    "TOP",

                "execution_state":
                    "HOLD",
            }


        active_ident = (
            request_identity(
                self.active
            )
        )


        if ident == active_ident:

            if fp == self.fingerprint:

                return {
                    "accepted":
                        True,

                    "duplicate":
                        True,

                    "reason_code":
                        None,

                    "expected_view":
                        "TOP",

                    "execution_state":
                        "HOLD",
                }


            return {
                "accepted":
                    False,

                "duplicate":
                    False,

                "reason_code":
                    "REQUEST_CONFLICT",
            }


        return {
            "accepted":
                False,

            "duplicate":
                False,

            "reason_code":
                "CONTROLLER_BUSY",
        }


# ============================================================
# SELF TEST
# ============================================================


def run_self_test():

    with tempfile.TemporaryDirectory() as td:

        store = GatewayStore(
            Path(td) /
            "gateway_v2.sqlite3"
        )

        controller = FakeController()

        source = (
            "192.168.20.20",
            41000,
        )


        request = {
            "ver":
                "0.1",

            "message_type":
                "pre_roof_inspection_request",

            "inspection_request_id":
                "550e8400-e29b-41d4-a716-446655440000",

            "inspection_cycle":
                1,

            "job_id":
                123,

            "job_code":
                "JOB-20260905-001",

            "inspection_type":
                "PRE_ROOF",

            "timestamp":
                "2026-09-05T08:30:00.000Z",
        }


        first = process_datagram(
            raw=compact_json_bytes(
                request
            ),
            source_addr=source,
            store=store,
            controller_submit=(
                controller.submit
            ),
        )

        assert (
            first[
                "outcome"
            ]
            ==
            "ACCEPTED"
        )

        assert (
            first[
                "ack"
            ][
                "accepted"
            ]
            is True
        )

        assert (
            first[
                "ack"
            ][
                "duplicate"
            ]
            is False
        )

        print(
            "TEST 1 CONTROLLER ACCEPT    : PASS"
        )


        duplicate = process_datagram(
            raw=compact_json_bytes(
                request
            ),
            source_addr=source,
            store=store,
            controller_submit=(
                controller.submit
            ),
        )

        assert (
            duplicate[
                "outcome"
            ]
            ==
            "DUPLICATE"
        )

        assert (
            duplicate[
                "ack"
            ][
                "duplicate"
            ]
            is True
        )

        print(
            "TEST 2 DUPLICATE            : PASS"
        )


        conflict_request = dict(
            request
        )

        conflict_request[
            "job_code"
        ] = "JOB-CONFLICT"


        conflict = process_datagram(
            raw=compact_json_bytes(
                conflict_request
            ),
            source_addr=source,
            store=store,
            controller_submit=(
                controller.submit
            ),
        )


        assert (
            conflict[
                "outcome"
            ]
            ==
            "REQUEST_CONFLICT"
        )

        assert (
            conflict[
                "ack"
            ][
                "reason_code"
            ]
            ==
            "REQUEST_CONFLICT"
        )

        print(
            "TEST 3 REQUEST CONFLICT     : PASS"
        )


        other = dict(
            request
        )

        other[
            "inspection_request_id"
        ] = (
            "123e4567-e89b-12d3-a456-426614174000"
        )

        other[
            "inspection_cycle"
        ] = 2


        busy = process_datagram(
            raw=compact_json_bytes(
                other
            ),
            source_addr=source,
            store=store,
            controller_submit=(
                controller.submit
            ),
        )


        assert (
            busy[
                "outcome"
            ]
            ==
            "CONTROLLER_REJECTED"
        )

        assert (
            busy[
                "ack"
            ][
                "accepted"
            ]
            is False
        )

        assert (
            busy[
                "ack"
            ][
                "reason_code"
            ]
            ==
            "CONTROLLER_BUSY"
        )

        print(
            "TEST 4 CONTROLLER BUSY      : PASS"
        )


        assert (
            store.transaction_count()
            ==
            1
        )

        row = store.lookup(
            request[
                "inspection_request_id"
            ],
            1,
        )

        assert (
            int(
                row[
                    "receive_count"
                ]
            )
            ==
            2
        )

        print(
            "TEST 5 SQLITE IDEMPOTENCY   : PASS"
        )


        store.close()


    print()
    print(
        "============================================"
    )

    print(
        " PRE_ROOF UDP GATEWAY V2 SELF TEST PASS"
    )

    print(
        "============================================"
    )

    print(
        "Controller-before-ACK : IMPLEMENTED"
    )

    print(
        "Controller URL        : "
        "http://127.0.0.1:8813/api/server-request"
    )

    print(
        "Request UDP           : "
        "192.168.20.30:20061"
    )

    print(
        "Final Result TX       : NOT YET"
    )

    print(
        "vision_production_valid=false"
    )


# ============================================================
# LIVE SERVER
# ============================================================


def run_gateway(
    *,
    bind_host,
    request_port,
    db_path,
    controller_url,
    result_host,
    result_port,
):

    store = GatewayStore(
        db_path
    )


    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )


    sock.bind(
        (
            bind_host,
            request_port,
        )
    )


    def controller_submit(
        request_payload,
    ):

        return submit_controller_http(
            request_payload,
            controller_url,
        )


    print()
    print(
        "============================================================"
    )

    print(
        " HARMONY PRE_ROOF UDP GATEWAY V2"
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
        f"RESULT TARGET  : "
        f"{result_host}:{result_port}/UDP"
    )

    print(
        "RESULT TX      : "
        "NOT YET CONNECTED"
    )

    print(
        f"SQLITE         : "
        f"{db_path}"
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
                sock.recvfrom(
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

                    sock.sendto(
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
                    f"SOURCE={source[0]}:{source[1]} "
                    f"OUTCOME={result['outcome']} "
                    f"REQUEST_ID={request_id} "
                    f"CYCLE={cycle} "
                    f"ACK={'YES' if ack else 'NO'}"
                )


            except Exception as exc:

                store.log_event(
                    event_type=(
                        "GATEWAY_INTERNAL_ERROR"
                    ),
                    source_addr=(
                        source
                    ),
                    detail={
                        "error":
                            repr(
                                exc
                            ),
                    },
                )


                print(
                    f"{now_utc()} "
                    f"SOURCE={source[0]}:{source[1]} "
                    f"OUTCOME=INTERNAL_ERROR "
                    f"ERROR={type(exc).__name__}"
                )


    except KeyboardInterrupt:

        print()
        print(
            "Gateway V2 stopped by operator."
        )


    finally:

        sock.close()
        store.close()


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
        result_host=(
            args.result_host
        ),
        result_port=(
            args.result_port
        ),
    )


if __name__ == "__main__":
    main()
