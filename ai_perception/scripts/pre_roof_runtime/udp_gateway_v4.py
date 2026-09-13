#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.request
from datetime import datetime, timezone


# ============================================================
# HARMONY PRE-ROOF PER-VIEW UDP GATEWAY V4
#
# SERVER
#   -> one View request
#
# Vision Dashboard
#   -> operator confirms current PASS / FAIL
#
# Gateway
#   -> sends that View result immediately
#
# Vision never chooses the next View.
# Server owns PASS/FAIL branching and next request.
# ============================================================


DEFAULT_BIND_HOST = "192.168.20.30"
DEFAULT_REQUEST_PORT = 20061

DEFAULT_RESULT_HOST = "192.168.20.20"
DEFAULT_RESULT_PORT = 20062

DASHBOARD_BASE = "http://127.0.0.1:8811"

VIEW_ORDER = (
    "TOP",
    "LEFT",
    "RIGHT",
    "FRONT",
    "BEHIND",
)

PROFILE_MAP = {
    "TOP": {
        "runtime_version": "V7",
        "runtime_port": 8775,
    },
    "LEFT": {
        "runtime_version": "V4",
        "runtime_port": 8777,
    },
    "RIGHT": {
        "runtime_version": "V8",
        "runtime_port": 8785,
    },
    "FRONT": {
        "runtime_version": "V2",
        "runtime_port": 8787,
    },
    "BEHIND": {
        "runtime_version": "V5",
        "runtime_port": 8792,
    },
}

MAX_DATAGRAM = 65535
POLL_SEC = 0.20


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
            "Z"
        )
    )


def compact_bytes(obj):
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def http_json(
    path,
    method="GET",
    body=None,
    timeout=3.0,
):
    url = (
        DASHBOARD_BASE +
        path
    )

    data = None

    headers = {}

    if body is not None:
        data = compact_bytes(
            body
        )

        headers[
            "Content-Type"
        ] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout,
    ) as r:
        raw = r.read()

    return json.loads(
        raw.decode(
            "utf-8"
        )
    )


def dashboard_state():
    return http_json(
        "/api/state"
    )


def activate_view(view):
    return http_json(
        "/api/activate",
        method="POST",
        body={
            "view": view,
        },
    )


def validate_request(obj):

    if not isinstance(
        obj,
        dict,
    ):
        raise ValueError(
            "request must be object"
        )

    message_type = str(
        obj.get(
            "message_type",
            ""
        )
    )

    if message_type not in (
        "pre_roof_view_inspection_request",
        "pre_roof_inspection_request",
    ):
        raise ValueError(
            "invalid message_type"
        )

    request_id = str(
        obj.get(
            "inspection_request_id",
            ""
        )
    ).strip()

    if not request_id:
        raise ValueError(
            "inspection_request_id required"
        )

    try:
        cycle = int(
            obj.get(
                "inspection_cycle"
            )
        )

    except Exception:
        raise ValueError(
            "inspection_cycle must be integer"
        )

    if cycle < 1:
        raise ValueError(
            "inspection_cycle must be >= 1"
        )

    view = str(
        obj.get(
            "view_name",
            obj.get(
                "view",
                ""
            )
        )
    ).upper().strip()

    if view not in VIEW_ORDER:
        raise ValueError(
            "view_name must be "
            "TOP/LEFT/RIGHT/FRONT/BEHIND"
        )

    out = dict(
        obj
    )

    out[
        "inspection_request_id"
    ] = request_id

    out[
        "inspection_cycle"
    ] = cycle

    out[
        "view_name"
    ] = view

    return out


class Gateway:

    def __init__(
        self,
        bind_host,
        request_port,
        result_host,
        result_port,
    ):

        self.bind_host = bind_host
        self.request_port = int(
            request_port
        )

        self.result_host = result_host
        self.result_port = int(
            result_port
        )

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.bind(
            (
                self.bind_host,
                self.request_port,
            )
        )

        self.lock = threading.RLock()

        self.active = None

        # Local idempotency for this runtime session.
        self.completed = {}


    def key_for(
        self,
        request,
    ):
        return (
            request[
                "inspection_request_id"
            ],
            int(
                request[
                    "inspection_cycle"
                ]
            ),
            request[
                "view_name"
            ],
        )


    def send_ack(
        self,
        source,
        request,
        accepted,
        reason_code=None,
        duplicate=False,
    ):

        payload = {
            "ver": "0.2",
            "message_type":
                "pre_roof_view_inspection_ack",

            "accepted":
                bool(
                    accepted
                ),

            "duplicate":
                bool(
                    duplicate
                ),

            "inspection_request_id":
                request.get(
                    "inspection_request_id"
                ),

            "inspection_cycle":
                request.get(
                    "inspection_cycle"
                ),

            "view_name":
                request.get(
                    "view_name"
                ),

            "reason_code":
                reason_code,

            "timestamp":
                now_utc(),
        }

        self.sock.sendto(
            compact_bytes(
                payload
            ),
            source,
        )


    def build_result(
        self,
        request,
        result,
    ):

        view = request[
            "view_name"
        ]

        profile = (
            PROFILE_MAP[
                view
            ]
        )

        return {
            "ver": "0.2",

            "message_type":
                "pre_roof_view_inspection_result",

            "inspection_request_id":
                request[
                    "inspection_request_id"
                ],

            "inspection_cycle":
                int(
                    request[
                        "inspection_cycle"
                    ]
                ),

            "job_id":
                request.get(
                    "job_id"
                ),

            "job_code":
                request.get(
                    "job_code"
                ),

            "inspection_type":
                "PRE_ROOF",

            "view_name":
                view,

            "result":
                result,

            "runtime_version":
                profile[
                    "runtime_version"
                ],

            "runtime_port":
                profile[
                    "runtime_port"
                ],

            "production_valid":
                False,

            "timestamp":
                now_utc(),
        }


    def send_result(
        self,
        payload,
    ):

        self.sock.sendto(
            compact_bytes(
                payload
            ),
            (
                self.result_host,
                self.result_port,
            ),
        )


    def watcher(
        self,
        request,
        key,
    ):

        view = request[
            "view_name"
        ]

        print(
            f"[WATCH] "
            f"{view} "
            f"REQUEST_ID="
            f"{request['inspection_request_id']}",
            flush=True,
        )

        while True:

            time.sleep(
                POLL_SEC
            )

            with self.lock:

                if (
                    self.active is None
                    or
                    self.active[
                        "key"
                    ] != key
                ):
                    return

            try:
                state = (
                    dashboard_state()
                )

            except Exception as e:

                print(
                    f"[WATCH] DASHBOARD ERROR: "
                    f"{e}",
                    flush=True,
                )

                continue

            current = (
                state.get(
                    "current_view"
                )
            )

            view_state = (
                state.get(
                    "views",
                    {}
                ).get(
                    view
                )
            )

            # While operator is inspecting.
            if (
                current == view
                or
                view_state == "ACTIVE"
            ):
                continue

            # Commit completed.
            if view_state not in (
                "PASS",
                "FAIL",
            ):
                continue

            result_payload = (
                self.build_result(
                    request,
                    view_state,
                )
            )

            self.send_result(
                result_payload
            )

            with self.lock:

                self.completed[
                    key
                ] = result_payload

                if (
                    self.active is not None
                    and
                    self.active[
                        "key"
                    ] == key
                ):
                    self.active = None

            print(
                f"[RESULT_SENT] "
                f"{view}={view_state} "
                f"-> "
                f"{self.result_host}:"
                f"{self.result_port}",
                flush=True,
            )

            return


    def process(
        self,
        raw,
        source,
    ):

        try:

            obj = json.loads(
                raw.decode(
                    "utf-8"
                )
            )

            request = (
                validate_request(
                    obj
                )
            )

        except Exception as e:

            payload = {
                "ver": "0.2",
                "message_type":
                    "pre_roof_view_inspection_ack",

                "accepted":
                    False,

                "duplicate":
                    False,

                "reason_code":
                    "INVALID_REQUEST",

                "message":
                    str(
                        e
                    ),

                "timestamp":
                    now_utc(),
            }

            self.sock.sendto(
                compact_bytes(
                    payload
                ),
                source,
            )

            print(
                f"[REJECT] {source} "
                f"{e}",
                flush=True,
            )

            return

        key = self.key_for(
            request
        )

        view = request[
            "view_name"
        ]

        with self.lock:

            previous = (
                self.completed.get(
                    key
                )
            )

            if previous is not None:

                self.send_ack(
                    source,
                    request,
                    accepted=True,
                    duplicate=True,
                )

                self.send_result(
                    previous
                )

                print(
                    f"[DUPLICATE] "
                    f"{view} result resent",
                    flush=True,
                )

                return

            if self.active is not None:

                active_key = (
                    self.active[
                        "key"
                    ]
                )

                if active_key == key:

                    self.send_ack(
                        source,
                        request,
                        accepted=True,
                        duplicate=True,
                    )

                    return

                self.send_ack(
                    source,
                    request,
                    accepted=False,
                    reason_code=
                        "VISION_BUSY",
                )

                print(
                    f"[BUSY] active="
                    f"{self.active['request']['view_name']} "
                    f"incoming={view}",
                    flush=True,
                )

                return

        # Server request selects the view.
        try:

            activation = (
                activate_view(
                    view
                )
            )

        except Exception as e:

            self.send_ack(
                source,
                request,
                accepted=False,
                reason_code=
                    "DASHBOARD_OFFLINE",
            )

            print(
                f"[REJECT] "
                f"DASHBOARD_OFFLINE "
                f"{e}",
                flush=True,
            )

            return

        if not activation.get(
            "ok",
            False,
        ):

            self.send_ack(
                source,
                request,
                accepted=False,
                reason_code=
                    "VIEW_ACTIVATION_REJECTED",
            )

            print(
                f"[REJECT] "
                f"{view}: "
                f"{activation.get('message')}",
                flush=True,
            )

            return

        with self.lock:

            self.active = {
                "key":
                    key,

                "request":
                    request,

                "source":
                    source,
            }

        self.send_ack(
            source,
            request,
            accepted=True,
        )

        print(
            f"[ACCEPT] "
            f"{source} "
            f"{view} "
            f"ID="
            f"{request['inspection_request_id']}",
            flush=True,
        )

        t = threading.Thread(
            target=self.watcher,
            args=(
                request,
                key,
            ),
            daemon=True,
            name=(
                f"watch-{view.lower()}"
            ),
        )

        t.start()


    def run(
        self,
    ):

        print(
            "============================================================",
            flush=True,
        )

        print(
            "HARMONY PRE-ROOF PER-VIEW UDP GATEWAY V4",
            flush=True,
        )

        print(
            f"REQUEST LISTEN : "
            f"{self.bind_host}:"
            f"{self.request_port}/UDP",
            flush=True,
        )

        print(
            "ACK TARGET     : REQUEST SOURCE IP/PORT",
            flush=True,
        )

        print(
            f"RESULT TARGET  : "
            f"{self.result_host}:"
            f"{self.result_port}/UDP",
            flush=True,
        )

        print(
            f"DASHBOARD      : "
            f"{DASHBOARD_BASE}",
            flush=True,
        )

        print(
            "MODE           : SERVER PER-VIEW REQUEST / OPERATOR COMMIT",
            flush=True,
        )

        print(
            "SERVER OWNS    : NEXT VIEW / FAIL REINSPECTION",
            flush=True,
        )

        print(
            "vision_production_valid=false",
            flush=True,
        )

        print(
            "============================================================",
            flush=True,
        )

        while True:

            raw, source = (
                self.sock.recvfrom(
                    MAX_DATAGRAM
                )
            )

            self.process(
                raw,
                source,
            )


def parse_args():

    p = argparse.ArgumentParser()

    p.add_argument(
        "--bind-host",
        default=DEFAULT_BIND_HOST,
    )

    p.add_argument(
        "--request-port",
        type=int,
        default=DEFAULT_REQUEST_PORT,
    )

    p.add_argument(
        "--result-host",
        default=DEFAULT_RESULT_HOST,
    )

    p.add_argument(
        "--result-port",
        type=int,
        default=DEFAULT_RESULT_PORT,
    )

    return p.parse_args()


def main():

    args = parse_args()

    Gateway(
        bind_host=args.bind_host,
        request_port=args.request_port,
        result_host=args.result_host,
        result_port=args.result_port,
    ).run()


if __name__ == "__main__":
    main()
