#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from urllib.parse import urlparse

import argparse
import json
import sys
import threading
import urllib.request

from datetime import (
    datetime,
    timezone,
)


# ============================================================
# PATH / CONTRACT
# ============================================================


ROOT = Path.home() / "vision_project"

CONTRACT_DIR = (
    ROOT /
    "scripts/pre_roof_runtime"
)

if str(CONTRACT_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(CONTRACT_DIR),
    )


from contracts_v1 import (
    ContractError,
    VIEW_ORDER,
    build_final_result,
    request_fingerprint,
    request_identity,
    validate_request,
)


# ============================================================
# HARMONY PRE_ROOF INTEGRATION CONTROLLER V3
#
# SERVER_MANUAL_5VIEW
#
# IMPLEMENTED
# - Server Request correlation
# - HOLD / INSPECT gate
# - Manual TOP -> LEFT -> RIGHT -> FRONT -> BEHIND
# - Runtime status acquisition
# - PASS / FAIL / NOT_EVALUATED normalization
# - ERROR -> NOT_EVALUATED normalization
# - 5-view Overall
# - Final Result Ready
#
# NOT YET
# - Final Result UDP transmission
# - Unity annotated publisher
#
# Robot motion is manual/external.
# ============================================================


CONTROLLER_VERSION = "V3"

PORT = 8814

BASE = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc"
)

STATE_DIR = (
    BASE /
    "integration/pre_roof_controller_v3"
)

STATE_PATH = (
    STATE_DIR /
    "state.json"
)

LOCK = threading.RLock()

PERSIST_ENABLED = True

RUNTIME_PROBE_OVERRIDE = None


# ============================================================
# FINAL ACTIVE PROFILES
# ============================================================


def load_profiles():

    profiles = {}

    for view in VIEW_ORDER:

        path = (
            BASE /
            f"ACTIVE_VIEW_{view}.json"
        )

        if not path.exists():
            raise RuntimeError(
                f"missing active pointer: {path}"
            )

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if (
            data.get("status")
            !=
            "FINAL_ACTIVE"
        ):
            raise RuntimeError(
                f"{view} is not FINAL_ACTIVE"
            )

        if (
            data.get(
                "production_valid"
            )
            is not False
        ):
            raise RuntimeError(
                f"{view} production_valid "
                "must remain false"
            )

        profiles[view] = {
            "runtime_version":
                str(
                    data[
                        "runtime_version"
                    ]
                ),

            "runtime_port":
                int(
                    data[
                        "runtime_port"
                    ]
                ),

            "robot_pose":
                data.get(
                    "robot_pose"
                ),

            "canonical_transform":
                data.get(
                    "canonical_transform"
                ),
        }

    return profiles


PROFILES = load_profiles()


# ============================================================
# TIME
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


# ============================================================
# STATE
# ============================================================


def waiting_views():

    return {
        view: {
            "state":
                "WAITING",

            "result":
                None,

            "reason_code":
                None,

            "runtime_raw_result":
                None,

            "runtime_reason":
                None,

            "committed_at":
                None,
        }
        for view in VIEW_ORDER
    }


def default_state():

    return {
        "schema":
            "harmony_pre_roof_"
            "integration_controller_state_v3",

        "controller_version":
            CONTROLLER_VERSION,

        "mode":
            "SERVER_MANUAL_5VIEW",

        "execution_state":
            "HOLD",

        "hold_reason":
            "IDLE",

        "transaction_state":
            "IDLE",

        "cycle_active":
            False,

        "active_request":
            None,

        "request_fingerprint":
            None,

        "expected_view":
            None,

        "current_view":
            None,

        "views":
            waiting_views(),

        "overall_result":
            None,

        "final_result":
            None,

        "final_result_ready":
            False,

        "vision_production_valid":
            False,

        "started_at":
            None,

        "completed_at":
            None,

        "events":
            [],
    }


def load_state():

    if not STATE_PATH.exists():
        return default_state()

    try:

        data = json.loads(
            STATE_PATH.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default_state()

    if (
        data.get("schema")
        !=
        "harmony_pre_roof_"
        "integration_controller_state_v3"
    ):
        return default_state()

    data[
        "vision_production_valid"
    ] = False

    return data


STATE = load_state()


def save_state():

    if not PERSIST_ENABLED:
        return

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = STATE_PATH.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            STATE,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    tmp.replace(
        STATE_PATH
    )


def add_event(
    event_type,
    **fields,
):

    item = {
        "timestamp":
            now_utc(),

        "event_type":
            event_type,
    }

    item.update(
        fields
    )

    STATE[
        "events"
    ].append(
        item
    )


# ============================================================
# SERVER REQUEST
# ============================================================


def initialize_transaction(
    request,
    fingerprint,
):

    global STATE

    STATE = default_state()

    STATE[
        "transaction_state"
    ] = "ACCEPTED"

    STATE[
        "cycle_active"
    ] = True

    STATE[
        "active_request"
    ] = request

    STATE[
        "request_fingerprint"
    ] = fingerprint

    STATE[
        "expected_view"
    ] = VIEW_ORDER[0]

    STATE[
        "execution_state"
    ] = "HOLD"

    STATE[
        "hold_reason"
    ] = "READY_FOR_VIEW"

    STATE[
        "started_at"
    ] = now_utc()

    add_event(
        "SERVER_REQUEST_ACCEPTED",
        inspection_request_id=(
            request[
                "inspection_request_id"
            ]
        ),
        inspection_cycle=(
            request[
                "inspection_cycle"
            ]
        ),
        job_id=(
            request[
                "job_id"
            ]
        ),
    )

    save_state()


def accept_server_request(
    payload,
):

    global STATE

    try:

        request = validate_request(
            payload
        )

    except ContractError as exc:

        return {
            "accepted":
                False,

            "duplicate":
                False,

            "reason_code":
                "INVALID_REQUEST",

            "message":
                str(exc),
        }


    fingerprint = (
        request_fingerprint(
            request
        )
    )

    request_id, cycle = (
        request_identity(
            request
        )
    )


    with LOCK:

        active = STATE.get(
            "active_request"
        )


        # ----------------------------------------------------
        # IDLE or previous cycle terminal
        # ----------------------------------------------------

        if (
            active is None
            or
            (
                not STATE.get(
                    "cycle_active"
                )
                and
                STATE.get(
                    "transaction_state"
                )
                ==
                "COMPLETED"
            )
        ):

            initialize_transaction(
                request,
                fingerprint,
            )

            return {
                "accepted":
                    True,

                "duplicate":
                    False,

                "reason_code":
                    None,

                "inspection_request_id":
                    request_id,

                "inspection_cycle":
                    cycle,

                "execution_state":
                    "HOLD",

                "expected_view":
                    "TOP",
            }


        active_id, active_cycle = (
            request_identity(
                active
            )
        )


        # ----------------------------------------------------
        # Same correlation
        # ----------------------------------------------------

        if (
            (
                active_id,
                active_cycle,
            )
            ==
            (
                request_id,
                cycle,
            )
        ):

            if (
                STATE.get(
                    "request_fingerprint"
                )
                ==
                fingerprint
            ):

                add_event(
                    "SERVER_REQUEST_DUPLICATE",
                    inspection_request_id=(
                        request_id
                    ),
                    inspection_cycle=(
                        cycle
                    ),
                )

                save_state()

                return {
                    "accepted":
                        True,

                    "duplicate":
                        True,

                    "reason_code":
                        None,

                    "inspection_request_id":
                        request_id,

                    "inspection_cycle":
                        cycle,

                    "execution_state":
                        STATE.get(
                            "execution_state"
                        ),

                    "expected_view":
                        STATE.get(
                            "expected_view"
                        ),
                }


            add_event(
                "SERVER_REQUEST_CONFLICT",
                inspection_request_id=(
                    request_id
                ),
                inspection_cycle=(
                    cycle
                ),
            )

            save_state()

            return {
                "accepted":
                    False,

                "duplicate":
                    False,

                "reason_code":
                    "REQUEST_CONFLICT",

                "inspection_request_id":
                    request_id,

                "inspection_cycle":
                    cycle,
            }


        # ----------------------------------------------------
        # Other transaction while active
        # ----------------------------------------------------

        add_event(
            "SERVER_REQUEST_BUSY",
            incoming_request_id=(
                request_id
            ),
            incoming_cycle=(
                cycle
            ),
            active_request_id=(
                active_id
            ),
            active_cycle=(
                active_cycle
            ),
        )

        save_state()

        return {
            "accepted":
                False,

            "duplicate":
                False,

            "reason_code":
                "CONTROLLER_BUSY",

            "active_inspection_request_id":
                active_id,

            "active_inspection_cycle":
                active_cycle,
        }


# ============================================================
# RUNTIME STATUS
# ============================================================


def runtime_status_http(
    view,
):

    port = (
        PROFILES[
            view
        ][
            "runtime_port"
        ]
    )

    url = (
        f"http://127.0.0.1:"
        f"{port}/status"
    )

    try:

        with urllib.request.urlopen(
            url,
            timeout=0.75,
        ) as response:

            raw = json.load(
                response
            )

        return {
            "online":
                True,

            "url":
                url,

            "raw":
                raw,
        }

    except Exception as exc:

        return {
            "online":
                False,

            "url":
                url,

            "error":
                type(exc).__name__,
        }


def runtime_status(
    view,
):

    if (
        RUNTIME_PROBE_OVERRIDE
        is not None
    ):
        return (
            RUNTIME_PROBE_OVERRIDE(
                view
            )
        )

    return runtime_status_http(
        view
    )


# ============================================================
# RUNTIME RESULT NORMALIZATION
# ============================================================


def normalize_runtime_result(
    runtime,
):

    if not runtime.get(
        "online",
        False,
    ):

        return {
            "commit":
                False,

            "block_reason":
                "RUNTIME_OFFLINE",
        }


    raw = runtime.get(
        "raw"
    )

    if not isinstance(
        raw,
        dict,
    ):

        return {
            "commit":
                False,

            "block_reason":
                "RESULT_NOT_READY",
        }


    result = raw.get(
        "result"
    )

    runtime_reason = (
        raw.get(
            "reason"
        )
        or
        raw.get(
            "error"
        )
    )


    if result == "PASS":

        return {
            "commit":
                True,

            "result":
                "PASS",

            "reason_code":
                None,

            "runtime_raw_result":
                "PASS",

            "runtime_reason":
                runtime_reason,
        }


    if result == "FAIL":

        return {
            "commit":
                True,

            "result":
                "FAIL",

            "reason_code":
                None,

            "runtime_raw_result":
                "FAIL",

            "runtime_reason":
                runtime_reason,
        }


    if result == "NOT_EVALUATED":

        # Runtime startup state is not a completed
        # inspection attempt.
        if (
            isinstance(
                runtime_reason,
                str,
            )
            and
            runtime_reason.strip().upper()
            ==
            "STARTING"
        ):

            return {
                "commit":
                    False,

                "block_reason":
                    "RESULT_NOT_READY",

                "runtime_raw_result":
                    result,

                "runtime_reason":
                    runtime_reason,
            }


        return {
            "commit":
                True,

            "result":
                "NOT_EVALUATED",

            "reason_code":
                "RUNTIME_NOT_EVALUATED",

            "runtime_raw_result":
                result,

            "runtime_reason":
                runtime_reason,
        }


    if result == "ERROR":

        return {
            "commit":
                True,

            "result":
                "NOT_EVALUATED",

            "reason_code":
                "RUNTIME_ERROR",

            "runtime_raw_result":
                "ERROR",

            "runtime_reason":
                runtime_reason,
        }


    return {
        "commit":
            False,

        "block_reason":
            "RESULT_NOT_READY",

        "runtime_raw_result":
            result,

        "runtime_reason":
            runtime_reason,
    }


# ============================================================
# FINAL RESULT
# ============================================================


def build_wire_final_result():

    request = STATE.get(
        "active_request"
    )

    if not isinstance(
        request,
        dict,
    ):
        raise RuntimeError(
            "active_request missing"
        )


    wire_views = []

    for view in VIEW_ORDER:

        item = (
            STATE[
                "views"
            ][
                view
            ]
        )

        result = item.get(
            "result"
        )

        if result not in (
            "PASS",
            "FAIL",
            "NOT_EVALUATED",
        ):
            raise RuntimeError(
                f"{view} not complete"
            )


        wire_views.append({
            "view_name":
                view,

            "result":
                result,

            "reason_code":
                item.get(
                    "reason_code"
                ),

            "defects":
                [],

            "metrics":
                {},
        })


    payload = build_final_result(
        request=request,
        views=wire_views,
        timestamp=now_utc(),
        vision_production_valid=False,
    )


    # Contract v0.1 structure remains unchanged.
    # Provenance value reflects the actually running
    # integration controller.
    payload[
        "runtime_versions"
    ][
        "controller"
    ] = CONTROLLER_VERSION


    return payload


# ============================================================
# INSPECT
# ============================================================


def inspect_expected_view():

    global STATE

    with LOCK:

        if not STATE.get(
            "cycle_active"
        ):

            return {
                "ok":
                    False,

                "reason_code":
                    "NO_ACTIVE_CYCLE",

                "message":
                    "진행 중인 PRE_ROOF 검사 사이클이 없습니다.",
            }


        if (
            STATE.get(
                "execution_state"
            )
            !=
            "HOLD"
        ):

            return {
                "ok":
                    False,

                "reason_code":
                    "INSPECT_BUSY",

                "message":
                    "현재 다른 검사가 실행 중입니다.",
            }


        view = STATE.get(
            "expected_view"
        )


        if view not in VIEW_ORDER:

            return {
                "ok":
                    False,

                "reason_code":
                    "NO_EXPECTED_VIEW",

                "message":
                    "검사할 다음 View가 없습니다.",
            }


        STATE[
            "execution_state"
        ] = "INSPECT"

        STATE[
            "hold_reason"
        ] = None

        STATE[
            "current_view"
        ] = view

        STATE[
            "views"
        ][
            view
        ][
            "state"
        ] = "INSPECTING"


        add_event(
            "VIEW_INSPECT_STARTED",
            view=view,
        )

        save_state()


    # Runtime HTTP is intentionally outside LOCK.
    runtime = runtime_status(
        view
    )

    normalized = (
        normalize_runtime_result(
            runtime
        )
    )


    with LOCK:

        # ----------------------------------------------------
        # Fail-safe: no valid result
        # ----------------------------------------------------

        if not normalized.get(
            "commit",
            False,
        ):

            block_reason = (
                normalized.get(
                    "block_reason"
                )
                or
                "RESULT_NOT_READY"
            )

            STATE[
                "execution_state"
            ] = "HOLD"

            STATE[
                "hold_reason"
            ] = block_reason

            STATE[
                "current_view"
            ] = None

            STATE[
                "views"
            ][
                view
            ][
                "state"
            ] = "WAITING"


            add_event(
                "VIEW_INSPECT_BLOCKED",
                view=view,
                reason_code=(
                    block_reason
                ),
                runtime_raw_result=(
                    normalized.get(
                        "runtime_raw_result"
                    )
                ),
                runtime_reason=(
                    normalized.get(
                        "runtime_reason"
                    )
                ),
            )

            save_state()


            return {
                "ok":
                    False,

                "reason_code":
                    block_reason,

                "view":
                    view,

                "expected_view":
                    view,

                "execution_state":
                    "HOLD",

                "message":
                    (
                        f"{view} 검사 결과를 "
                        "확정하지 않았습니다."
                    ),
            }


        # ----------------------------------------------------
        # Commit one View
        # ----------------------------------------------------

        result = normalized[
            "result"
        ]

        reason_code = (
            normalized.get(
                "reason_code"
            )
        )


        STATE[
            "views"
        ][
            view
        ] = {
            "state":
                result,

            "result":
                result,

            "reason_code":
                reason_code,

            "runtime_raw_result":
                normalized.get(
                    "runtime_raw_result"
                ),

            "runtime_reason":
                normalized.get(
                    "runtime_reason"
                ),

            "committed_at":
                now_utc(),
        }


        add_event(
            "VIEW_RESULT_COMMITTED",
            view=view,
            result=result,
            reason_code=reason_code,
            runtime_raw_result=(
                normalized.get(
                    "runtime_raw_result"
                )
            ),
            runtime_version=(
                PROFILES[
                    view
                ][
                    "runtime_version"
                ]
            ),
            runtime_port=(
                PROFILES[
                    view
                ][
                    "runtime_port"
                ]
            ),
        )


        index = (
            list(
                VIEW_ORDER
            ).index(
                view
            )
        )


        # ----------------------------------------------------
        # More Views remain
        # ----------------------------------------------------

        if index < (
            len(
                VIEW_ORDER
            )
            - 1
        ):

            next_view = (
                VIEW_ORDER[
                    index + 1
                ]
            )

            STATE[
                "expected_view"
            ] = next_view

            STATE[
                "current_view"
            ] = None

            STATE[
                "execution_state"
            ] = "HOLD"

            STATE[
                "hold_reason"
            ] = "BETWEEN_VIEWS"

            save_state()


            return {
                "ok":
                    True,

                "view":
                    view,

                "result":
                    result,

                "reason_code":
                    reason_code,

                "cycle_complete":
                    False,

                "execution_state":
                    "HOLD",

                "expected_view":
                    next_view,

                "message":
                    (
                        f"{view} 검사 완료. "
                        f"{next_view} 위치로 이동하세요."
                    ),
            }


        # ----------------------------------------------------
        # Fifth View -> Final Result Ready
        # ----------------------------------------------------

        final_payload = (
            build_wire_final_result()
        )


        STATE[
            "overall_result"
        ] = final_payload[
            "overall_result"
        ]

        STATE[
            "final_result"
        ] = final_payload

        STATE[
            "final_result_ready"
        ] = True

        STATE[
            "cycle_active"
        ] = False

        STATE[
            "transaction_state"
        ] = "COMPLETED"

        STATE[
            "expected_view"
        ] = None

        STATE[
            "current_view"
        ] = None

        STATE[
            "execution_state"
        ] = "HOLD"

        STATE[
            "hold_reason"
        ] = "CYCLE_COMPLETE"

        STATE[
            "completed_at"
        ] = now_utc()


        add_event(
            "FINAL_RESULT_READY",
            overall_result=(
                final_payload[
                    "overall_result"
                ]
            ),
        )

        save_state()


        return {
            "ok":
                True,

            "view":
                view,

            "result":
                result,

            "reason_code":
                reason_code,

            "cycle_complete":
                True,

            "overall_result":
                final_payload[
                    "overall_result"
                ],

            "final_result_ready":
                True,

            "execution_state":
                "HOLD",

            "expected_view":
                None,

            "message":
                (
                    "PRE_ROOF 5-View 검사 완료. "
                    f"Overall="
                    f"{final_payload['overall_result']}"
                ),
        }


# ============================================================
# RESET
# ============================================================


def reset_controller():

    global STATE

    with LOCK:

        STATE = default_state()

        add_event(
            "CONTROLLER_RESET"
        )

        save_state()

    return {
        "ok":
            True,

        "message":
            "Controller V3 state reset",
    }


# ============================================================
# STATUS
# ============================================================


def public_status():

    with LOCK:

        return json.loads(
            json.dumps(
                STATE
            )
        )


# ============================================================
# HTTP
# ============================================================


class Handler(
    BaseHTTPRequestHandler
):


    def log_message(
        self,
        fmt,
        *args,
    ):
        return


    def send_json(
        self,
        payload,
        status=200,
    ):

        raw = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ).encode(
            "utf-8"
        )

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(
                len(
                    raw
                )
            ),
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.end_headers()

        self.wfile.write(
            raw
        )


    def read_json(self):

        length = int(
            self.headers.get(
                "Content-Length",
                "0",
            )
        )

        raw = self.rfile.read(
            length
        )

        return json.loads(
            raw.decode(
                "utf-8"
            )
        )


    def do_GET(self):

        path = urlparse(
            self.path
        ).path


        if path in (
            "/status",
            "/api/state",
        ):

            self.send_json(
                public_status()
            )

            return


        if path == (
            "/api/final-result"
        ):

            state = (
                public_status()
            )

            self.send_json({
                "ready":
                    state[
                        "final_result_ready"
                    ],

                "final_result":
                    state[
                        "final_result"
                    ],
            })

            return


        self.send_json(
            {
                "error":
                    "NOT_FOUND"
            },
            404,
        )


    def do_POST(self):

        path = urlparse(
            self.path
        ).path


        if path == (
            "/api/server-request"
        ):

            try:

                payload = (
                    self.read_json()
                )

            except Exception:

                self.send_json(
                    {
                        "accepted":
                            False,

                        "duplicate":
                            False,

                        "reason_code":
                            "INVALID_JSON",
                    },
                    400,
                )

                return


            result = (
                accept_server_request(
                    payload
                )
            )

            self.send_json(
                result,
                (
                    200
                    if result[
                        "accepted"
                    ]
                    else 409
                ),
            )

            return


        if path == "/api/inspect":

            result = (
                inspect_expected_view()
            )

            self.send_json(
                result,
                (
                    200
                    if result[
                        "ok"
                    ]
                    else 409
                ),
            )

            return


        if path == "/api/reset":

            self.send_json(
                reset_controller()
            )

            return


        self.send_json(
            {
                "error":
                    "NOT_FOUND"
            },
            404,
        )


# ============================================================
# SELF TEST
# ============================================================


def run_self_test():

    global STATE
    global PERSIST_ENABLED
    global RUNTIME_PROBE_OVERRIDE


    old_state = STATE
    old_persist = PERSIST_ENABLED
    old_probe = RUNTIME_PROBE_OVERRIDE


    try:

        # IMPORTANT:
        # self-test must not touch persistent state.json.
        PERSIST_ENABLED = False

        STATE = default_state()


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
                "JOB-PRE-ROOF-V3-SELFTEST",

            "inspection_type":
                "PRE_ROOF",

            "timestamp":
                "2026-09-05T09:10:00.000Z",
        }


        accepted = (
            accept_server_request(
                request
            )
        )

        assert (
            accepted[
                "accepted"
            ]
            is True
        )

        assert (
            STATE[
                "expected_view"
            ]
            ==
            "TOP"
        )

        assert (
            STATE[
                "execution_state"
            ]
            ==
            "HOLD"
        )


        print(
            "TEST 1 REQUEST -> HOLD/TOP       : PASS"
        )


        # ----------------------------------------------------
        # TOP STARTING must not commit
        # ----------------------------------------------------

        def starting_probe(
            view
        ):

            return {
                "online":
                    True,

                "raw": {
                    "result":
                        "NOT_EVALUATED",

                    "reason":
                        "STARTING",
                },
            }


        RUNTIME_PROBE_OVERRIDE = (
            starting_probe
        )


        blocked = (
            inspect_expected_view()
        )


        assert (
            blocked[
                "ok"
            ]
            is False
        )

        assert (
            blocked[
                "reason_code"
            ]
            ==
            "RESULT_NOT_READY"
        )

        assert (
            STATE[
                "expected_view"
            ]
            ==
            "TOP"
        )

        assert (
            STATE[
                "views"
            ][
                "TOP"
            ][
                "state"
            ]
            ==
            "WAITING"
        )


        print(
            "TEST 2 STARTING BLOCK             : PASS"
        )


        # ----------------------------------------------------
        # Actual 5-view sequence
        #
        # TOP    PASS
        # LEFT   FAIL
        # RIGHT  ERROR -> NOT_EVALUATED
        # FRONT  PASS
        # BEHIND PASS
        #
        # Overall -> NOT_EVALUATED
        # ----------------------------------------------------

        fake = {
            "TOP": {
                "result":
                    "PASS",
            },

            "LEFT": {
                "result":
                    "FAIL",
            },

            "RIGHT": {
                "result":
                    "ERROR",

                "error":
                    "synthetic runtime error",
            },

            "FRONT": {
                "result":
                    "PASS",
            },

            "BEHIND": {
                "result":
                    "PASS",
            },
        }


        def fake_probe(
            view
        ):

            return {
                "online":
                    True,

                "raw":
                    fake[
                        view
                    ],
            }


        RUNTIME_PROBE_OVERRIDE = (
            fake_probe
        )


        r_top = (
            inspect_expected_view()
        )

        assert (
            r_top[
                "result"
            ]
            ==
            "PASS"
        )

        assert (
            r_top[
                "expected_view"
            ]
            ==
            "LEFT"
        )


        print(
            "TEST 3 TOP PASS -> LEFT           : PASS"
        )


        r_left = (
            inspect_expected_view()
        )

        assert (
            r_left[
                "result"
            ]
            ==
            "FAIL"
        )

        assert (
            r_left[
                "expected_view"
            ]
            ==
            "RIGHT"
        )


        print(
            "TEST 4 LEFT FAIL -> RIGHT         : PASS"
        )


        r_right = (
            inspect_expected_view()
        )

        assert (
            r_right[
                "result"
            ]
            ==
            "NOT_EVALUATED"
        )

        assert (
            r_right[
                "reason_code"
            ]
            ==
            "RUNTIME_ERROR"
        )

        assert (
            r_right[
                "expected_view"
            ]
            ==
            "FRONT"
        )


        print(
            "TEST 5 ERROR -> NOT_EVALUATED     : PASS"
        )


        r_front = (
            inspect_expected_view()
        )

        assert (
            r_front[
                "result"
            ]
            ==
            "PASS"
        )

        assert (
            r_front[
                "expected_view"
            ]
            ==
            "BEHIND"
        )


        print(
            "TEST 6 FRONT PASS -> BEHIND       : PASS"
        )


        r_behind = (
            inspect_expected_view()
        )

        assert (
            r_behind[
                "cycle_complete"
            ]
            is True
        )

        assert (
            r_behind[
                "overall_result"
            ]
            ==
            "NOT_EVALUATED"
        )


        assert (
            STATE[
                "transaction_state"
            ]
            ==
            "COMPLETED"
        )

        assert (
            STATE[
                "cycle_active"
            ]
            is False
        )

        assert (
            STATE[
                "final_result_ready"
            ]
            is True
        )

        assert (
            STATE[
                "execution_state"
            ]
            ==
            "HOLD"
        )

        assert (
            STATE[
                "hold_reason"
            ]
            ==
            "CYCLE_COMPLETE"
        )


        print(
            "TEST 7 BEHIND -> FINAL READY       : PASS"
        )


        final_result = (
            STATE[
                "final_result"
            ]
        )


        assert (
            final_result[
                "overall_result"
            ]
            ==
            "NOT_EVALUATED"
        )


        assert (
            final_result[
                "vision_production_valid"
            ]
            is False
        )


        assert (
            final_result[
                "runtime_versions"
            ][
                "controller"
            ]
            ==
            "V3"
        )


        assert (
            len(
                final_result[
                    "views"
                ]
            )
            ==
            5
        )


        print(
            "TEST 8 FINAL WIRE PAYLOAD         : PASS"
        )


        # ----------------------------------------------------
        # A new Request can start after terminal completion.
        # ----------------------------------------------------

        request2 = dict(
            request
        )

        request2[
            "inspection_request_id"
        ] = (
            "123e4567-e89b-12d3-a456-426614174000"
        )

        request2[
            "inspection_cycle"
        ] = 2

        request2[
            "timestamp"
        ] = (
            "2026-09-05T09:15:00.000Z"
        )


        accepted2 = (
            accept_server_request(
                request2
            )
        )


        assert (
            accepted2[
                "accepted"
            ]
            is True
        )

        assert (
            accepted2[
                "duplicate"
            ]
            is False
        )

        assert (
            STATE[
                "expected_view"
            ]
            ==
            "TOP"
        )


        print(
            "TEST 9 REINSPECTION NEW CYCLE     : PASS"
        )


        print()
        print(
            "============================================"
        )

        print(
            " PRE_ROOF CONTROLLER V3 SELF TEST PASS"
        )

        print(
            "============================================"
        )

        print(
            "Server Request         : IMPLEMENTED"
        )

        print(
            "HOLD / INSPECT         : IMPLEMENTED"
        )

        print(
            "Manual 5-View          : IMPLEMENTED"
        )

        print(
            "ERROR normalization    : IMPLEMENTED"
        )

        print(
            "Overall                : IMPLEMENTED"
        )

        print(
            "Final Result Ready     : IMPLEMENTED"
        )

        print(
            "Final Result UDP TX    : NOT YET"
        )

        print(
            "vision_production_valid=false"
        )


    finally:

        STATE = old_state

        PERSIST_ENABLED = (
            old_persist
        )

        RUNTIME_PROBE_OVERRIDE = (
            old_probe
        )


# ============================================================
# MAIN
# ============================================================


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()


    if args.self_test:

        run_self_test()
        return


    print()
    print(
        "============================================================"
    )

    print(
        " HARMONY PRE_ROOF INTEGRATION CONTROLLER V3"
    )

    print(
        "============================================================"
    )

    print(
        "MODE                  : SERVER_MANUAL_5VIEW"
    )

    print(
        "HTTP                  : "
        "http://127.0.0.1:8814"
    )

    print(
        "SERVER REQUEST        : IMPLEMENTED"
    )

    print(
        "HOLD / INSPECT        : IMPLEMENTED"
    )

    print(
        "5-VIEW ORDER          : "
        + " -> ".join(
            VIEW_ORDER
        )
    )

    print(
        "ROBOT MOTION          : MANUAL / EXTERNAL"
    )

    print(
        "ERROR NORMALIZATION   : "
        "ERROR -> NOT_EVALUATED"
    )

    print(
        "FINAL RESULT READY    : IMPLEMENTED"
    )

    print(
        "FINAL RESULT UDP TX   : NOT YET"
    )

    print(
        "vision_production_valid=false"
    )

    print(
        "============================================================"
    )


    server = ThreadingHTTPServer(
        (
            "127.0.0.1",
            PORT,
        ),
        Handler,
    )

    server.serve_forever()


if __name__ == "__main__":
    main()
