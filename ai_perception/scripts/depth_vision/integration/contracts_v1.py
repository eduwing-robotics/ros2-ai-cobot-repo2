#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


# ============================================================
# HARMONY PRE_ROOF WIRE CONTRACT V0.1
#
# Vision <-> Team Server/FMS
#
# IMPORTANT
# - Incoming QA contract is NOT reused.
# - PRE_ROOF is an independent ProductionInspection contract.
# - One Request = one inspection_cycle = one 5-view Final Result.
# - production_valid authority remains Team Server/FMS.
# - Vision wire field is vision_production_valid.
# ============================================================


WIRE_VERSION = "0.1"

REQUEST_MESSAGE_TYPE = (
    "pre_roof_inspection_request"
)

ACK_MESSAGE_TYPE = (
    "pre_roof_inspection_ack"
)

RESULT_MESSAGE_TYPE = (
    "pre_roof_inspection_result"
)

INSPECTION_TYPE = "PRE_ROOF"

RUNTIME_PROFILE = "PRE_ROOF_5VIEW"

VIEW_ORDER = (
    "TOP",
    "LEFT",
    "RIGHT",
    "FRONT",
    "BEHIND",
)

VALID_RESULTS = {
    "PASS",
    "FAIL",
    "NOT_EVALUATED",
}

RUNTIME_VERSIONS = {
    "controller": "V1",
    "TOP": "V5",
    "LEFT": "V3",
    "RIGHT": "V7",
    "FRONT": "V1",
    "BEHIND": "V4",
}


# ACK rejection reasons.
#
# Keep v0.1 deliberately small.
ACK_REASON_INVALID_REQUEST = (
    "INVALID_REQUEST"
)

ACK_REASON_REQUEST_CONFLICT = (
    "REQUEST_CONFLICT"
)

ACK_REASON_INTERNAL_ERROR = (
    "INTERNAL_ERROR"
)


# NOT_EVALUATED wire normalization.
VIEW_REASON_RUNTIME_NOT_EVALUATED = (
    "RUNTIME_NOT_EVALUATED"
)

VIEW_REASON_RUNTIME_ERROR = (
    "RUNTIME_ERROR"
)


RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?Z$"
)


class ContractError(
    ValueError
):
    pass


# ============================================================
# TIME
# ============================================================


def now_utc_rfc3339():

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


def validate_timestamp(
    value,
):

    if not isinstance(
        value,
        str,
    ):
        raise ContractError(
            "timestamp must be string"
        )

    if not RFC3339_UTC_RE.fullmatch(
        value
    ):
        raise ContractError(
            "timestamp must be UTC RFC3339"
        )

    try:

        datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

    except Exception as exc:

        raise ContractError(
            "invalid timestamp"
        ) from exc

    return value


# ============================================================
# BASIC VALIDATORS
# ============================================================


def validate_uuid(
    value,
):

    if not isinstance(
        value,
        str,
    ):
        raise ContractError(
            "inspection_request_id "
            "must be string UUID"
        )

    try:

        parsed = uuid.UUID(
            value
        )

    except Exception as exc:

        raise ContractError(
            "invalid inspection_request_id"
        ) from exc

    canonical = str(
        parsed
    )

    if canonical != value.lower():
        raise ContractError(
            "inspection_request_id "
            "must use canonical UUID form"
        )

    return canonical


def validate_positive_int(
    value,
    field,
):

    if (
        isinstance(
            value,
            bool,
        )
        or
        not isinstance(
            value,
            int,
        )
        or
        value < 1
    ):
        raise ContractError(
            f"{field} must be integer >= 1"
        )

    return value


def validate_nonempty_string(
    value,
    field,
):

    if (
        not isinstance(
            value,
            str,
        )
        or
        not value.strip()
    ):
        raise ContractError(
            f"{field} must be non-empty string"
        )

    return value


# ============================================================
# REQUEST
# ============================================================


REQUEST_KEYS = {
    "ver",
    "message_type",
    "inspection_request_id",
    "inspection_cycle",
    "job_id",
    "job_code",
    "inspection_type",
    "timestamp",
}


def validate_request(
    payload,
):

    if not isinstance(
        payload,
        dict,
    ):
        raise ContractError(
            "request payload must be object"
        )

    keys = set(
        payload.keys()
    )

    if keys != REQUEST_KEYS:

        missing = sorted(
            REQUEST_KEYS - keys
        )

        extra = sorted(
            keys - REQUEST_KEYS
        )

        raise ContractError(
            "request fields mismatch; "
            f"missing={missing}; "
            f"extra={extra}"
        )


    if payload["ver"] != WIRE_VERSION:
        raise ContractError(
            "unsupported wire version"
        )


    if (
        payload["message_type"]
        != REQUEST_MESSAGE_TYPE
    ):
        raise ContractError(
            "invalid request message_type"
        )


    request_id = validate_uuid(
        payload[
            "inspection_request_id"
        ]
    )


    cycle = validate_positive_int(
        payload[
            "inspection_cycle"
        ],
        "inspection_cycle",
    )


    job_id = validate_positive_int(
        payload[
            "job_id"
        ],
        "job_id",
    )


    job_code = validate_nonempty_string(
        payload[
            "job_code"
        ],
        "job_code",
    )


    if (
        payload[
            "inspection_type"
        ]
        != INSPECTION_TYPE
    ):
        raise ContractError(
            "inspection_type must be PRE_ROOF"
        )


    timestamp = validate_timestamp(
        payload[
            "timestamp"
        ]
    )


    return {
        "ver":
            WIRE_VERSION,

        "message_type":
            REQUEST_MESSAGE_TYPE,

        "inspection_request_id":
            request_id,

        "inspection_cycle":
            cycle,

        "job_id":
            job_id,

        "job_code":
            job_code,

        "inspection_type":
            INSPECTION_TYPE,

        "timestamp":
            timestamp,
    }


# ============================================================
# CANONICAL REQUEST / IDEMPOTENCY FINGERPRINT
# ============================================================


def canonical_json_bytes(
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


def request_fingerprint(
    normalized_request,
):

    return hashlib.sha256(
        canonical_json_bytes(
            normalized_request
        )
    ).hexdigest()


def request_identity(
    normalized_request,
):

    return (
        normalized_request[
            "inspection_request_id"
        ],
        normalized_request[
            "inspection_cycle"
        ],
    )


# ============================================================
# ACK
# ============================================================


def build_ack(
    *,
    inspection_request_id,
    inspection_cycle,
    accepted,
    duplicate,
    reason_code,
):

    validate_uuid(
        inspection_request_id
    )

    validate_positive_int(
        inspection_cycle,
        "inspection_cycle",
    )

    if not isinstance(
        accepted,
        bool,
    ):
        raise ContractError(
            "accepted must be boolean"
        )

    if not isinstance(
        duplicate,
        bool,
    ):
        raise ContractError(
            "duplicate must be boolean"
        )

    if (
        reason_code is not None
        and
        not isinstance(
            reason_code,
            str,
        )
    ):
        raise ContractError(
            "reason_code must be string or null"
        )


    if accepted:

        if reason_code is not None:
            raise ContractError(
                "accepted ACK must have "
                "reason_code=null"
            )

    else:

        if duplicate:
            raise ContractError(
                "rejected ACK cannot be duplicate"
            )

        if not reason_code:
            raise ContractError(
                "rejected ACK requires reason_code"
            )


    return {
        "ver":
            WIRE_VERSION,

        "message_type":
            ACK_MESSAGE_TYPE,

        "inspection_request_id":
            inspection_request_id,

        "inspection_cycle":
            inspection_cycle,

        "accepted":
            accepted,

        "duplicate":
            duplicate,

        "reason_code":
            reason_code,
    }


# ============================================================
# VIEW RESULT
# ============================================================


def normalize_view_result(
    *,
    view_name,
    result,
    reason_code=None,
    defects=None,
    metrics=None,
):

    if view_name not in VIEW_ORDER:
        raise ContractError(
            f"invalid view_name: {view_name}"
        )

    if result not in VALID_RESULTS:
        raise ContractError(
            f"invalid view result: {result}"
        )


    if defects is None:
        defects = []

    if metrics is None:
        metrics = {}


    if not isinstance(
        defects,
        list,
    ):
        raise ContractError(
            "defects must be array"
        )

    if not isinstance(
        metrics,
        dict,
    ):
        raise ContractError(
            "metrics must be object"
        )


    if result == "NOT_EVALUATED":

        if not isinstance(
            reason_code,
            str,
        ) or not reason_code:
            raise ContractError(
                "NOT_EVALUATED requires "
                "reason_code"
            )

    elif reason_code is not None:

        if not isinstance(
            reason_code,
            str,
        ):
            raise ContractError(
                "reason_code must be "
                "string or null"
            )


    return {
        "view_name":
            view_name,

        "result":
            result,

        "reason_code":
            reason_code,

        "defects":
            defects,

        "metrics":
            metrics,
    }


# ============================================================
# OVERALL
# ============================================================


def calculate_overall(
    view_results,
):

    if not isinstance(
        view_results,
        dict,
    ):
        raise ContractError(
            "view_results must be object"
        )


    if set(
        view_results.keys()
    ) != set(
        VIEW_ORDER
    ):
        return None


    values = [
        view_results[
            view
        ]
        for view in VIEW_ORDER
    ]


    if any(
        value not in VALID_RESULTS
        for value in values
    ):
        return None


    if "NOT_EVALUATED" in values:
        return "NOT_EVALUATED"


    if "FAIL" in values:
        return "FAIL"


    if all(
        value == "PASS"
        for value in values
    ):
        return "PASS"


    return None


# ============================================================
# FINAL RESULT
# ============================================================


def build_final_result(
    *,
    request,
    views,
    timestamp=None,
    vision_production_valid=False,
):

    request = validate_request(
        request
    )


    if not isinstance(
        views,
        list,
    ):
        raise ContractError(
            "views must be array"
        )


    if len(views) != len(
        VIEW_ORDER
    ):
        raise ContractError(
            "Final Result requires "
            "exactly 5 views"
        )


    normalized_views = []

    seen = set()

    for item in views:

        if not isinstance(
            item,
            dict,
        ):
            raise ContractError(
                "view entry must be object"
            )

        normalized = (
            normalize_view_result(
                view_name=item.get(
                    "view_name"
                ),
                result=item.get(
                    "result"
                ),
                reason_code=item.get(
                    "reason_code"
                ),
                defects=item.get(
                    "defects",
                    [],
                ),
                metrics=item.get(
                    "metrics",
                    {},
                ),
            )
        )

        view_name = normalized[
            "view_name"
        ]

        if view_name in seen:
            raise ContractError(
                f"duplicate view: {view_name}"
            )

        seen.add(
            view_name
        )

        normalized_views.append(
            normalized
        )


    if seen != set(
        VIEW_ORDER
    ):
        raise ContractError(
            "Final Result must contain "
            "TOP/LEFT/RIGHT/FRONT/BEHIND"
        )


    # Canonical view ordering.
    by_name = {
        item["view_name"]:
            item
        for item in normalized_views
    }

    normalized_views = [
        by_name[view]
        for view in VIEW_ORDER
    ]


    overall = calculate_overall({
        item["view_name"]:
            item["result"]
        for item in normalized_views
    })


    if overall is None:
        raise ContractError(
            "overall result is not ready"
        )


    if timestamp is None:
        timestamp = (
            now_utc_rfc3339()
        )

    validate_timestamp(
        timestamp
    )


    if not isinstance(
        vision_production_valid,
        bool,
    ):
        raise ContractError(
            "vision_production_valid "
            "must be boolean"
        )


    return {
        "ver":
            WIRE_VERSION,

        "message_type":
            RESULT_MESSAGE_TYPE,

        "inspection_request_id":
            request[
                "inspection_request_id"
            ],

        "inspection_cycle":
            request[
                "inspection_cycle"
            ],

        "job_id":
            request[
                "job_id"
            ],

        "inspection_type":
            INSPECTION_TYPE,

        "status":
            "COMPLETED",

        "overall_result":
            overall,

        "vision_production_valid":
            vision_production_valid,

        "views":
            normalized_views,

        "timestamp":
            timestamp,

        "runtime_profile":
            RUNTIME_PROFILE,

        "runtime_versions":
            dict(
                RUNTIME_VERSIONS
            ),
    }


# ============================================================
# FINAL RESULT FINGERPRINT
# ============================================================


def result_fingerprint(
    result_payload,
):

    return hashlib.sha256(
        canonical_json_bytes(
            result_payload
        )
    ).hexdigest()


# ============================================================
# SELF TEST
# ============================================================


def run_self_test():

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


    normalized = validate_request(
        request
    )

    print(
        "TEST 1 VALID REQUEST       : PASS"
    )


    fingerprint = request_fingerprint(
        normalized
    )

    if len(fingerprint) != 64:
        raise SystemExit(
            "TEST 2 FAIL"
        )

    print(
        "TEST 2 REQUEST FINGERPRINT : PASS"
    )


    ack = build_ack(
        inspection_request_id=(
            normalized[
                "inspection_request_id"
            ]
        ),
        inspection_cycle=(
            normalized[
                "inspection_cycle"
            ]
        ),
        accepted=True,
        duplicate=False,
        reason_code=None,
    )

    if (
        ack["accepted"] is not True
        or
        ack["duplicate"] is not False
    ):
        raise SystemExit(
            "TEST 3 FAIL"
        )

    print(
        "TEST 3 ACK                 : PASS"
    )


    pass_views = [
        normalize_view_result(
            view_name=view,
            result="PASS",
        )
        for view in VIEW_ORDER
    ]


    result = build_final_result(
        request=normalized,
        views=pass_views,
        timestamp=(
            "2026-09-05T08:35:30.123Z"
        ),
        vision_production_valid=False,
    )


    if (
        result[
            "overall_result"
        ]
        != "PASS"
    ):
        raise SystemExit(
            "TEST 4 FAIL"
        )

    print(
        "TEST 4 OVERALL PASS        : PASS"
    )


    fail_views = [
        normalize_view_result(
            view_name=view,
            result=(
                "FAIL"
                if view == "RIGHT"
                else "PASS"
            ),
        )
        for view in VIEW_ORDER
    ]


    result = build_final_result(
        request=normalized,
        views=fail_views,
        vision_production_valid=False,
    )

    if (
        result[
            "overall_result"
        ]
        != "FAIL"
    ):
        raise SystemExit(
            "TEST 5 FAIL"
        )

    print(
        "TEST 5 OVERALL FAIL        : PASS"
    )


    ne_views = [
        normalize_view_result(
            view_name=view,
            result=(
                "NOT_EVALUATED"
                if view == "LEFT"
                else "PASS"
            ),
            reason_code=(
                VIEW_REASON_RUNTIME_NOT_EVALUATED
                if view == "LEFT"
                else None
            ),
        )
        for view in VIEW_ORDER
    ]


    result = build_final_result(
        request=normalized,
        views=ne_views,
        vision_production_valid=False,
    )

    if (
        result[
            "overall_result"
        ]
        != "NOT_EVALUATED"
    ):
        raise SystemExit(
            "TEST 6 FAIL"
        )

    print(
        "TEST 6 NOT_EVALUATED       : PASS"
    )


    try:

        bad = dict(
            request
        )

        bad[
            "inspection_request_id"
        ] = "not-a-uuid"

        validate_request(
            bad
        )

        raise SystemExit(
            "TEST 7 FAIL"
        )

    except ContractError:
        pass

    print(
        "TEST 7 INVALID UUID BLOCK  : PASS"
    )


    try:

        normalize_view_result(
            view_name="TOP",
            result="NOT_EVALUATED",
            reason_code=None,
        )

        raise SystemExit(
            "TEST 8 FAIL"
        )

    except ContractError:
        pass

    print(
        "TEST 8 NE REASON REQUIRED  : PASS"
    )


    print()
    print(
        "============================================"
    )

    print(
        " PRE_ROOF WIRE CONTRACT V0.1 SELF TEST PASS"
    )

    print(
        "============================================"
    )

    print(
        "Request UDP : "
        "192.168.20.30:20061"
    )

    print(
        "Result UDP  : "
        "192.168.20.20:20062"
    )

    print(
        "vision_production_valid=false"
    )


if __name__ == "__main__":

    run_self_test()
