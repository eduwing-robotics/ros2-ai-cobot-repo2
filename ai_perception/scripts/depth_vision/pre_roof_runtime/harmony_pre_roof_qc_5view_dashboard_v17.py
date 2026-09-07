#!/usr/bin/env python3

from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
import threading
import datetime
import json
import html


ROOT = Path.home() / "vision_project"

BASE = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc"
)

STATE_DIR = (
    BASE /
    "integration/5view_dashboard_v2"
)

STATE_PATH = (
    STATE_DIR /
    "state.json"
)

PORT = 8811

ORDER = [
    "TOP",
    "LEFT",
    "RIGHT",
    "FRONT",
    "BEHIND",
]

SCHEMA = (
    "harmony_pre_roof_5view_dashboard_v2"
)

STATE_SCHEMA = (
    "harmony_pre_roof_5view_dashboard_state_v2"
)

PRODUCTION_VALID = False

STATE_LOCK = threading.RLock()


# =========================================================
# ACTIVE PROFILE LOAD
# =========================================================

def load_profiles():

    profiles = {}

    for view in ORDER:

        p = (
            BASE /
            f"ACTIVE_VIEW_{view}.json"
        )

        if not p.exists():
            raise RuntimeError(
                f"Missing active pointer: {p}"
            )

        d = json.loads(
            p.read_text(
                encoding="utf-8"
            )
        )

        status = (
            d.get("status")
            or
            d.get("state")
        )

        if status != "FINAL_ACTIVE":
            raise RuntimeError(
                f"{view} is not FINAL_ACTIVE: "
                f"{status}"
            )

        if d.get("production_valid") is not False:
            raise RuntimeError(
                f"{view}: unexpected "
                f"production_valid value"
            )

        profiles[view] = {
            "view":
                view,

            "runtime":
                d.get("runtime"),

            "runtime_version":
                d.get("runtime_version"),

            "runtime_port":
                d.get("runtime_port"),

            "robot_pose":
                d.get("robot_pose"),

            "canonical_transform":
                d.get("canonical_transform"),

            "final_lock":
                d.get("final_lock"),

            "production_valid":
                False,
        }

    return profiles


PROFILES = load_profiles()


# =========================================================
# STATE
# =========================================================

def default_state():

    return {
        "schema":
            STATE_SCHEMA,

        "sequence":
            list(ORDER),

        "current_view":
            None,

        "views": {
            view: "WAITING"
            for view in ORDER
        },

        "attempts":
            [],

        "production_valid":
            False,
    }


def save_state(state):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = (
        STATE_PATH.with_suffix(
            ".json.tmp"
        )
    )

    tmp.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ) + "\n",
        encoding="utf-8"
    )

    tmp.replace(
        STATE_PATH
    )


def load_state():

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not STATE_PATH.exists():

        state = default_state()

        save_state(
            state
        )

        return state

    state = json.loads(
        STATE_PATH.read_text(
            encoding="utf-8"
        )
    )

    if state.get("schema") != STATE_SCHEMA:
        raise RuntimeError(
            "Unexpected dashboard state schema"
        )

    return state


STATE = load_state()


# =========================================================
# STATE HELPERS
# =========================================================

def now_iso():

    return (
        datetime.datetime.now(
            datetime.timezone.utc
        )
        .astimezone()
        .isoformat(
            timespec="seconds"
        )
    )


def final_result(state):

    values = [
        state["views"][v]
        for v in ORDER
    ]

    if any(
        x in (
            "WAITING",
            "ACTIVE",
        )
        for x in values
    ):
        return "WAITING"

    if all(
        x == "PASS"
        for x in values
    ):
        return "PASS"

    return "FAIL"


def expected_next(state):

    for view in ORDER:

        if (
            state["views"][view]
            == "WAITING"
        ):
            return view

    return None


# =========================================================
# INDIVIDUAL RUNTIME STATUS
# =========================================================

def runtime_status(view):

    profile = PROFILES[view]

    port = profile[
        "runtime_port"
    ]

    url = (
        f"http://127.0.0.1:"
        f"{port}/status"
    )

    try:

        with urllib.request.urlopen(
            url,
            timeout=0.45
        ) as r:

            raw = json.load(r)

        return {
            "online":
                True,

            "result":
                raw.get("result"),

            "schema":
                raw.get("schema"),

            "url":
                url,
        }

    except Exception as e:

        return {
            "online":
                False,

            "result":
                None,

            "schema":
                None,

            "url":
                url,

            "error":
                type(e).__name__,
        }


# =========================================================
# INTEGRATED STATUS
# =========================================================

def integrated_status():

    with STATE_LOCK:

        state = json.loads(
            json.dumps(STATE)
        )

    current = (
        state.get(
            "current_view"
        )
    )

    runtime = None

    if current:

        runtime = runtime_status(
            current
        )

    return {
        "schema":
            SCHEMA,

        "inspection":
            "PRE_ROOF",

        "sequence":
            list(ORDER),

        "current_view":
            current,

        "expected_next":
            expected_next(
                state
            ),

        "views":
            state["views"],

        "final_result":
            final_result(
                state
            ),

        "active_runtime":
            runtime,

        "profiles":
            PROFILES,

        "attempt_count":
            len(
                state.get(
                    "attempts",
                    []
                )
            ),

        "production_valid":
            False,
    }


# =========================================================
# CONTROL
# =========================================================

def activate_view(view):

    global STATE

    if view not in ORDER:

        return (
            False,
            f"알 수 없는 검사 위치: {view}"
        )

    with STATE_LOCK:

        current = (
            STATE.get(
                "current_view"
            )
        )

        if current:

            if current == view:
                return (
                    True,
                    f"{view} 검사가 이미 진행 중입니다."
                )

            return (
                False,
                f"{current} 검사가 이미 진행 중입니다."
            )

        status = (
            STATE["views"][view]
        )

        next_view = (
            expected_next(
                STATE
            )
        )

        # A failed view may be reactivated
        # for reinspection.
        if status == "FAIL":

            pass

        elif status == "WAITING":

            if view != next_view:

                return (
                    False,
                    f"다음 검사 위치는 "
                    f"{next_view} 입니다."
                )

        elif status == "PASS":

            return (
                False,
                f"{view} 검사는 이미 정상으로 확정되었습니다."
            )

        else:

            return (
                False,
                f"{view} 검사를 시작할 수 없습니다. 현재 상태: "
                f"from {status}"
            )

        STATE[
            "views"
        ][view] = "ACTIVE"

        STATE[
            "current_view"
        ] = view

        save_state(
            STATE
        )

        return (
            True,
            f"{view} 검사 시작"
        )


def activate_next():

    with STATE_LOCK:

        view = expected_next(
            STATE
        )

    if view is None:

        return (
            False,
            "대기 중인 검사 위치가 없습니다."
        )

    return activate_view(
        view
    )


def commit_current():

    global STATE

    with STATE_LOCK:

        view = (
            STATE.get(
                "current_view"
            )
        )

    if not view:

        return (
            False,
            "현재 진행 중인 검사가 없습니다."
        )

    runtime = runtime_status(
        view
    )

    if not runtime[
        "online"
    ]:

        return (
            False,
            f"{view} 검사 모듈이 실행되지 않았습니다."
        )

    result = (
        runtime.get(
            "result"
        )
    )

    if result not in (
        "PASS",
        "FAIL",
    ):

        return (
            False,
            f"{view} 검사 결과를 확정할 수 없습니다. 현재 결과: "
            f"is not PASS/FAIL: {result}"
        )

    with STATE_LOCK:

        # Verify state did not change
        # during HTTP polling.
        if (
            STATE.get(
                "current_view"
            )
            != view
        ):
            return (
                False,
                "결과 확정 중 검사 위치가 변경되었습니다."
            )

        STATE[
            "views"
        ][view] = result

        STATE[
            "current_view"
        ] = None

        STATE.setdefault(
            "attempts",
            []
        ).append({
            "timestamp":
                now_iso(),

            "view":
                view,

            "result":
                result,

            "runtime_version":
                PROFILES[view][
                    "runtime_version"
                ],

            "runtime_port":
                PROFILES[view][
                    "runtime_port"
                ],
        })

        save_state(
            STATE
        )

    return (
        True,
        f"{view} 검사 결과 확정: {result}"
    )


def reset_sequence():

    global STATE

    with STATE_LOCK:

        STATE = default_state()

        save_state(
            STATE
        )

    return (
        True,
        "검사 순서를 초기화했습니다."
    )


# =========================================================
# WEB UI
# =========================================================

PAGE = r"""<!doctype html>
<html lang="ko">

<head>
<meta charset="utf-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1">

<title>Harmony PRE-ROOF 5방향 품질 검사</title>

<style>

:root {
    --bg: #0b0f14;
    --surface: #11171e;
    --surface2: #171e26;
    --surface3: #1d2630;
    --border: #2b3642;

    --text: #eef3f7;
    --muted: #8493a3;
    --muted2: #657383;

    --accent: #53b8e8;

    --waiting: #8b98a5;
    --active: #f0b84b;
    --pass: #45d483;
    --fail: #f05d67;

    --radius: 12px;
}

* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    min-height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family:
        Pretendard,
        "Noto Sans KR",
        "Malgun Gothic",
        Arial,
        sans-serif;
}

body {
    min-height: 100vh;
}

.app {
    width: min(1980px, calc(100% - 20px));
    margin: 0 auto;
    padding-bottom: 22px;
}


/* =======================================================
   HEADER
   ======================================================= */

.header {
    height: 78px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
}

.brand-kicker {
    font-size: 11px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 1.6px;
    margin-bottom: 4px;
}

.title {
    font-size: 27px;
    line-height: 1.15;
    font-weight: 800;
    letter-spacing: -0.5px;
}

.subtitle {
    margin-top: 5px;
    color: var(--muted);
    font-size: 13px;
}

.header-right {
    display: flex;
    align-items: center;
    gap: 10px;
}

.profile-chip {
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 9px;
    text-align: right;
}

.profile-chip .a {
    font-size: 13px;
    font-weight: 700;
}

.profile-chip .b {
    margin-top: 3px;
    font-size: 10px;
    color: var(--muted);
}


/* =======================================================
   PROCESS STAGES
   ======================================================= */

.process {
    padding: 14px 0 12px;
}

.process-label {
    display: flex;
    justify-content: space-between;
    margin-bottom: 8px;
}

.process-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--muted);
}

.progress-text {
    color: var(--muted);
    font-size: 11px;
}

.progress-track {
    height: 3px;
    border-radius: 99px;
    background: #1b232c;
    margin-bottom: 11px;
    overflow: hidden;
}

.progress-bar {
    height: 100%;
    width: 0%;
    border-radius: 99px;
    background: var(--accent);
    transition: width 0.25s ease;
}

.cards {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 8px;
}

.stage-card {
    min-width: 0;
    padding: 11px 12px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background:
        linear-gradient(
            180deg,
            #171e26 0%,
            #141a21 100%
        );
    cursor: pointer;
    position: relative;
    transition:
        transform .12s ease,
        border-color .12s ease,
        background .12s ease;
}

.stage-card:hover {
    border-color: #536170;
    transform: translateY(-1px);
}

.stage-card.ACTIVE {
    border-color:
        color-mix(
            in srgb,
            var(--active) 65%,
            var(--border)
        );
}

.stage-card.PASS {
    border-color:
        color-mix(
            in srgb,
            var(--pass) 50%,
            var(--border)
        );
}

.stage-card.FAIL {
    border-color:
        color-mix(
            in srgb,
            var(--fail) 65%,
            var(--border)
        );
}

.stage-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 6px;
}

.step {
    font-size: 10px;
    font-weight: 800;
    color: var(--muted2);
    letter-spacing: 1px;
}

.stage-name {
    margin-top: 5px;
    font-size: 16px;
    font-weight: 800;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.state-pill {
    flex: none;
    padding: 5px 8px;
    border-radius: 99px;
    font-size: 10px;
    font-weight: 800;
    background: #202933;
    color: var(--waiting);
}

.state-pill.ACTIVE {
    background: rgba(240,184,75,.12);
    color: var(--active);
}

.state-pill.PASS {
    background: rgba(69,212,131,.12);
    color: var(--pass);
}

.state-pill.FAIL {
    background: rgba(240,93,103,.12);
    color: var(--fail);
}

.stage-tech {
    margin-top: 9px;
    font-size: 10px;
    color: var(--muted2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}


/* =======================================================
   WORKSPACE
   ======================================================= */

.workspace {
    display: grid;
    grid-template-columns:
        minmax(0, 1fr)
        350px;
    gap: 10px;
    align-items: start;
}


/* =======================================================
   LIVE INSPECTION
   ======================================================= */

.live-panel {
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
}

.live-header {
    height: 49px;
    padding: 0 15px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
}

.live-title {
    display: flex;
    align-items: center;
    gap: 9px;
}

.live-title-main {
    font-size: 14px;
    font-weight: 800;
}

.live-title-sub {
    font-size: 10px;
    color: var(--muted);
    margin-top: 2px;
}

.connection {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
}

.dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--waiting);
}

.dot.online {
    background: var(--pass);
    box-shadow:
        0 0 0 3px
        rgba(69,212,131,.10);
}

.dot.offline {
    background: var(--fail);
    box-shadow:
        0 0 0 3px
        rgba(240,93,103,.10);
}

.viewport {
    position: relative;

    /*
       Full-screen / 4K stretch guard.
       The view grows only to 650 px high.
    */
    height: clamp(
        500px,
        65vh,
        750px
    );

    background:
        radial-gradient(
            circle at 50% 45%,
            #0e141a,
            #070a0e 70%
        );
}

iframe {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: #070a0e;
}

.offline {
    position: absolute;
    inset: 0;
    z-index: 5;

    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;

    padding: 30px;

    text-align: center;
    color: var(--muted);

    background:
        radial-gradient(
            circle at 50% 45%,
            #10171e,
            #070a0e 72%
        );
}

.offline-icon {
    width: 44px;
    height: 44px;
    display: grid;
    place-items: center;
    margin-bottom: 14px;
    border-radius: 50%;
    border: 1px solid #36414d;
    color: #9ca9b5;
    font-size: 18px;
}

.offline-title {
    color: #d8e0e7;
    font-size: 16px;
    font-weight: 800;
}

.offline-text {
    margin-top: 8px;
    max-width: 520px;
    font-size: 12px;
    line-height: 1.65;
}


/* =======================================================
   INSPECTOR
   ======================================================= */

.inspector {
    display: flex;
    flex-direction: column;
    gap: 9px;
}

.inspector-card {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    padding: 13px;
}

.inspector-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 11px;
}

.inspector-heading span:first-child {
    font-size: 14px;
    font-weight: 800;
}

.inspector-tag {
    font-size: 9px;
    color: var(--accent);
    border: 1px solid #284b5d;
    padding: 3px 6px;
    border-radius: 99px;
}

.info-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid #222b34;
}

.info-row:last-child {
    border-bottom: 0;
}

.info-label {
    font-size: 11px;
    color: var(--muted);
}

.info-value {
    max-width: 195px;
    text-align: right;
    font-size: 13px;
    font-weight: 750;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}

.quality-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 7px;
}

.q-box {
    padding: 9px;
    border-radius: 8px;
    border: 1px solid #27323d;
    background: #131a21;
}

.q-label {
    font-size: 10px;
    color: var(--muted);
}

.q-value {
    margin-top: 6px;
    font-size: 19px;
    font-weight: 850;
}

.q-value.pass {
    color: var(--pass);
}

.q-value.fail {
    color: var(--fail);
}

.q-value.wait {
    color: var(--waiting);
}

.final-box {
    padding: 12px;
    border: 1px solid #34404c;
    border-radius: 9px;
    background:
        linear-gradient(
            135deg,
            #171f27,
            #131920
        );
}

.final-label {
    font-size: 10px;
    color: var(--muted);
}

.final-value {
    margin-top: 7px;
    font-size: 22px;
    font-weight: 900;
}

.WAITING {
    color: var(--waiting);
}

.ACTIVE {
    color: var(--active);
}

.PASS {
    color: var(--pass);
}

.FAIL {
    color: var(--fail);
}


/* =======================================================
   BUTTONS
   ======================================================= */

.controls {
    display: grid;
    gap: 6px;
    margin-top: 9px;
}

button {
    width: 100%;
    height: 39px;
    border: 1px solid #35414d;
    border-radius: 7px;

    background:
        linear-gradient(
            180deg,
            #222b34,
            #1b232b
        );

    color: #e9eef3;
    font-family: inherit;
    font-size: 11px;
    font-weight: 800;

    cursor: pointer;
}

button:hover {
    background: #293440;
    border-color: #536170;
}

button.primary {
    border-color: #35657d;
    background:
        linear-gradient(
            180deg,
            #1c4458,
            #173747
        );
}

.message {
    min-height: 22px;
    padding-top: 9px;
    color: var(--muted);
    font-size: 10px;
    line-height: 1.45;
}

.note {
    font-size: 10px;
    line-height: 1.55;
    color: var(--muted2);
}


/* =======================================================
   FOOTER
   ======================================================= */

.footer {
    padding-top: 11px;
    display: flex;
    justify-content: space-between;
    color: #596775;
    font-size: 9px;
}


/* =======================================================
   RESPONSIVE
   ======================================================= */

@media (max-width: 1250px) {

    .workspace {
        grid-template-columns: 1fr;
    }

    .inspector {
        display: grid;
        grid-template-columns:
            1fr 1fr;
    }

    .viewport {
        height: clamp(
            480px,
            62vh,
            700px
        );
    }
}

@media (max-width: 900px) {

    .cards {
        grid-template-columns:
            repeat(2, 1fr);
    }

    .inspector {
        grid-template-columns: 1fr;
    }
}

</style>

<style>

/* =======================================================
   V6 LARGE DISPLAY VISUAL OVERRIDE
   PRESENTATION ONLY
   ======================================================= */

.app {
    width:
        min(
            3200px,
            calc(100% - 36px)
        );
}


/* HEADER */

.header {
    height: 104px;
}

.brand-kicker {
    font-size: 14px;
    letter-spacing: 2px;
}

.title {
    font-size: 36px;
}

.subtitle {
    margin-top: 7px;
    font-size: 16px;
}

.profile-chip {
    padding: 12px 16px;
}

.profile-chip .a {
    font-size: 16px;
}

.profile-chip .b {
    font-size: 12px;
}


/* PROCESS */

.process {
    padding-top: 20px;
    padding-bottom: 16px;
}

.process-title {
    font-size: 16px;
}

.progress-text {
    font-size: 14px;
}

.progress-track {
    height: 5px;
    margin-bottom: 15px;
}

.cards {
    gap: 12px;
}

.stage-card {
    padding: 16px 17px;
    min-height: 104px;
}

.step {
    font-size: 13px;
}

.stage-name {
    margin-top: 7px;
    font-size: 21px;
}

.state-pill {
    padding: 6px 10px;
    font-size: 13px;
}

.stage-tech {
    margin-top: 11px;
    font-size: 13px;
}


/* WORKSPACE */

.workspace {
    grid-template-columns:
        minmax(0, 1fr)
        430px;

    gap: 14px;
}


/* LIVE HEADER */

.live-header {
    height: 66px;
    padding: 0 20px;
}

.live-title-main {
    font-size: 19px;
}

.live-title-sub {
    margin-top: 4px;
    font-size: 13px;
}

.connection {
    font-size: 13px;
    gap: 9px;
}

.dot {
    width: 10px;
    height: 10px;
}


/* MAIN VIEW */

.viewport {
    height:
        clamp(
            620px,
            72vh,
            980px
        );
}

.offline-icon {
    width: 58px;
    height: 58px;
    font-size: 24px;
    margin-bottom: 18px;
}

.offline-title {
    font-size: 21px;
}

.offline-text {
    margin-top: 10px;
    max-width: 700px;
    font-size: 16px;
    line-height: 1.7;
}


/* INSPECTOR */

.inspector {
    gap: 13px;
}

.inspector-card {
    padding: 18px;
}

.inspector-heading {
    margin-bottom: 15px;
}

.inspector-heading span:first-child {
    font-size: 18px;
}

.inspector-tag {
    font-size: 11px;
    padding: 5px 8px;
}

.info-row {
    padding: 12px 0;
}

.info-label {
    font-size: 14px;
}

.info-value {
    max-width: 245px;
    font-size: 16px;
}

.quality-grid {
    gap: 10px;
}

.q-box {
    padding: 13px;
}

.q-label {
    font-size: 13px;
}

.q-value {
    margin-top: 7px;
    font-size: 26px;
}

.final-box {
    padding: 17px;
}

.final-label {
    font-size: 13px;
}

.final-value {
    margin-top: 8px;
    font-size: 30px;
}


/* CONTROLS */

.controls {
    gap: 9px;
    margin-top: 12px;
}

button {
    height: 48px;
    font-size: 14px;
    border-radius: 9px;
}

.message {
    min-height: 27px;
    padding-top: 10px;
    font-size: 13px;
}

.note {
    font-size: 13px;
    line-height: 1.65;
}


/* FOOTER */

.footer {
    padding-top: 14px;
    font-size: 11px;
}


/* =======================================================
   NORMAL FHD / LAPTOP GUARD
   ======================================================= */

@media (max-width: 2200px) {

    .app {
        width:
            min(
                2100px,
                calc(100% - 24px)
            );
    }

    .workspace {
        grid-template-columns:
            minmax(0, 1fr)
            380px;
    }

    .viewport {
        height:
            clamp(
                540px,
                66vh,
                820px
            );
    }

    .title {
        font-size: 31px;
    }

    .stage-name {
        font-size: 18px;
    }

    .inspector-heading span:first-child {
        font-size: 16px;
    }

    .info-label {
        font-size: 12px;
    }

    .info-value {
        font-size: 14px;
    }
}

</style>


<style>

/* =======================================================
   V7 CAMERA VIEW ASPECT LOCK
   VISUAL ONLY
   D435 DISPLAY BASIS: 1280 x 720 = 16:9
   ======================================================= */

.viewport {
    width: 100%;

    /*
       Match the actual RGB / Depth display ratio.
       Prevent the inspection area from becoming
       a wide panoramic rectangle.
    */
    aspect-ratio: 16 / 9;

    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
}


/* iframe and offline overlay remain exactly fitted
   to the 16:9 inspection viewport. */

.viewport > iframe,
.viewport > .offline {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
}


/* Keep the live panel aligned from the top. */

.live-panel {
    align-self: start;
}


/* Small-screen guard */

@media (max-width: 1250px) {

    .viewport {
        width: 100%;
        aspect-ratio: 16 / 9;
        height: auto !important;
    }
}

</style>


<style>

/* =======================================================
   V8 FONT TUNE
   VISUAL ONLY
   NO LAYOUT / VIEWPORT / API CHANGE
   ======================================================= */


/* Header */

.brand-kicker {
    font-size: 15px;
}

.title {
    font-size: 39px;
}

.subtitle {
    font-size: 17px;
}

.profile-chip .a {
    font-size: 17px;
}

.profile-chip .b {
    font-size: 13px;
}


/* Process */

.process-title {
    font-size: 17px;
}

.progress-text {
    font-size: 15px;
}


/* 5-view cards */

.step {
    font-size: 14px;
}

.stage-name {
    font-size: 23px;
}

.state-pill {
    font-size: 14px;
}

.stage-tech {
    font-size: 14px;
}


/* Live inspection header */

.live-title-main {
    font-size: 21px;
}

.live-title-sub {
    font-size: 14px;
}

.connection {
    font-size: 14px;
}


/* Center message */

.offline-title {
    font-size: 23px;
}

.offline-text {
    font-size: 17px;
}


/* Inspector */

.inspector-heading span:first-child {
    font-size: 20px;
}

.inspector-tag {
    font-size: 12px;
}

.info-label {
    font-size: 15px;
}

.info-value {
    font-size: 18px;
}


/* Quality summary */

.q-label {
    font-size: 14px;
}

.q-value {
    font-size: 29px;
}

.final-label {
    font-size: 14px;
}

.final-value {
    font-size: 33px;
}


/* Controls */

button {
    font-size: 15px;
}

.message {
    font-size: 14px;
}

.note {
    font-size: 14px;
}


/* Footer */

.footer {
    font-size: 12px;
}


/* Smaller-display guard */

@media (max-width: 2200px) {

    .title {
        font-size: 34px;
    }

    .stage-name {
        font-size: 20px;
    }

    .info-label {
        font-size: 13px;
    }

    .info-value {
        font-size: 16px;
    }

    .q-value {
        font-size: 25px;
    }

    button {
        font-size: 13px;
    }
}

</style>


<style>

/* =======================================================
   V9 FONT X2 TEST
   VISUAL ONLY
   CURRENT LAYOUT / 16:9 VIEWPORT UNCHANGED
   ======================================================= */


/* HEADER */

.brand-kicker {
    font-size: 30px !important;
}

.title {
    font-size: 68px !important;
}

.subtitle {
    font-size: 34px !important;
}

.profile-chip .a {
    font-size: 34px !important;
}

.profile-chip .b {
    font-size: 26px !important;
}


/* PROCESS */

.process-title {
    font-size: 34px !important;
}

.progress-text {
    font-size: 30px !important;
}


/* 5-VIEW CARDS */

.step {
    font-size: 28px !important;
}

.stage-name {
    font-size: 40px !important;
}

.state-pill {
    font-size: 28px !important;
}

.stage-tech {
    font-size: 28px !important;
}


/* LIVE HEADER */

.live-title-main {
    font-size: 42px !important;
}

.live-title-sub {
    font-size: 28px !important;
}

.connection {
    font-size: 28px !important;
}


/* CENTER STATUS MESSAGE */

.offline-title {
    font-size: 46px !important;
}

.offline-text {
    font-size: 34px !important;
}


/* INSPECTOR */

.inspector-heading span:first-child {
    font-size: 40px !important;
}

.inspector-tag {
    font-size: 24px !important;
}

.info-label {
    font-size: 26px !important;
}

.info-value {
    font-size: 32px !important;
}


/* QUALITY SUMMARY */

.q-label {
    font-size: 28px !important;
}

.q-value {
    font-size: 50px !important;
}

.final-label {
    font-size: 28px !important;
}

.final-value {
    font-size: 66px !important;
}


/* CONTROLS */

button {
    font-size: 26px !important;
}

.message {
    font-size: 28px !important;
}

.note {
    font-size: 28px !important;
}


/* FOOTER */

.footer {
    font-size: 24px !important;
}

</style>


<style>

/* =======================================================
   V10 ANCHOR FINE TUNE
   VISUAL ALIGNMENT ONLY

   FONT SIZE     : V9 UNCHANGED
   VIEWPORT      : V9 UNCHANGED
   16:9          : UNCHANGED
   ======================================================= */


/* -------------------------------------------------------
   HEADER
   ------------------------------------------------------- */

.header {
    padding-left: 10px;
    padding-right: 8px;
}

.header > div:first-child {
    display: flex;
    flex-direction: column;
    justify-content: center;
}

.brand-kicker {
    margin: 0 0 3px 0;
    line-height: 1;
}

.title {
    margin: 0;
    line-height: 1.08;
}

.subtitle {
    margin-top: 6px;
    line-height: 1.15;
}

.header-right {
    align-self: center;
}

.profile-chip {
    display: flex;
    min-width: 275px;
    flex-direction: column;
    justify-content: center;
    align-items: flex-end;
}

.profile-chip .a,
.profile-chip .b {
    width: 100%;
    text-align: right;
}


/* -------------------------------------------------------
   PROCESS TITLE / PROGRESS
   ------------------------------------------------------- */

.process-label {
    min-height: 32px;
    padding: 0 4px;
    display: flex;
    align-items: center;
}

.process-title,
.progress-text {
    line-height: 1;
}

.progress-track {
    margin-top: 1px;
}


/* -------------------------------------------------------
   VIEW CARDS
   ------------------------------------------------------- */

.stage-card {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding:
        14px
        15px
        12px
        15px;
}

.stage-top {
    width: 100%;
    align-items: flex-start;
}

.stage-top > div:first-child {
    min-width: 0;
}

.step {
    line-height: 1;
}

.stage-name {
    line-height: 1.08;
}

.state-pill {
    margin-top: 1px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    white-space: nowrap;
}

.stage-tech {
    line-height: 1.1;
}


/* -------------------------------------------------------
   LIVE PANEL HEADER
   ------------------------------------------------------- */

.live-header {
    padding-left: 16px;
    padding-right: 16px;
}

.live-title {
    height: 100%;
    display: flex;
    align-items: center;
}

.live-title > div {
    display: flex;
    flex-direction: column;
    justify-content: center;
}

.live-title-main {
    line-height: 1.08;
}

.live-title-sub {
    line-height: 1.08;
}

.connection {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    line-height: 1;
}


/* -------------------------------------------------------
   CENTER OFFLINE MESSAGE
   ------------------------------------------------------- */

.offline {
    padding-bottom: 2%;
}

.offline-icon {
    flex: 0 0 auto;
}

.offline-title {
    line-height: 1.1;
}

.offline-text {
    line-height: 1.55;
}


/* -------------------------------------------------------
   INSPECTOR CARDS
   ------------------------------------------------------- */

.inspector-card {
    padding:
        17px
        16px
        16px
        16px;
}

.inspector-heading {
    min-height: 34px;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
}

.inspector-heading span:first-child {
    line-height: 1;
}

.inspector-tag {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
}


/* -------------------------------------------------------
   INSPECTOR INFO ROWS

   Fixed label/value columns for industrial-HMI alignment.
   ------------------------------------------------------- */

.info-row {
    display: grid;
    grid-template-columns:
        minmax(0, 1fr)
        minmax(145px, auto);

    align-items: center;

    min-height: 49px;

    padding:
        7px
        2px;

    column-gap: 15px;
}

.info-label {
    display: flex;
    align-items: center;
    line-height: 1.15;
}

.info-value {
    width: 100%;
    max-width: none;
    text-align: right;
    line-height: 1.15;
}


/* -------------------------------------------------------
   QUALITY SUMMARY 2x2 GRID
   ------------------------------------------------------- */

.quality-grid {
    grid-auto-rows: 92px;
}

.q-box {
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding:
        10px
        13px;
}

.q-label {
    line-height: 1;
}

.q-value {
    margin-top: 8px;
    line-height: 1;
}


/* -------------------------------------------------------
   FINAL RESULT BOX
   ------------------------------------------------------- */

.final-box {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 105px;
    padding:
        13px
        16px;
}

.final-label {
    line-height: 1;
}

.final-value {
    line-height: 1;
}


/* -------------------------------------------------------
   CONTROL BUTTONS
   ------------------------------------------------------- */

.controls {
    margin-top: 10px;
}

button {
    display: flex;
    align-items: center;
    justify-content: center;
    padding:
        0
        14px;

    line-height: 1;
}


/* -------------------------------------------------------
   MESSAGE / SYSTEM INFORMATION
   ------------------------------------------------------- */

.message {
    display: flex;
    align-items: center;
    line-height: 1.3;
}

.note {
    margin: 0;
    line-height: 1.5;
}


/* -------------------------------------------------------
   FOOTER
   ------------------------------------------------------- */

.footer {
    padding-left: 2px;
    padding-right: 2px;

    display: flex;
    align-items: center;
}

.footer span:first-child {
    text-align: left;
}

.footer span:last-child {
    margin-left: auto;
    text-align: right;
}

</style>


<style>

/* =======================================================
   V11 SPACIOUS ANCHOR LAYOUT

   Font sizes are inherited from V10 unchanged.
   Only dimensions / padding / spacing / anchors change.
   ======================================================= */


/* -------------------------------------------------------
   APP OUTER BREATHING SPACE
   ------------------------------------------------------- */

.app {
    padding-top: 8px;
    padding-bottom: 32px;
}


/* -------------------------------------------------------
   HEADER

   Give title / subtitle enough independent vertical space.
   ------------------------------------------------------- */

.header {
    height: 165px;

    padding:
        20px
        18px
        18px
        16px;

    align-items: center;
}

.header > div:first-child {
    height: 100%;

    display: flex;
    flex-direction: column;
    justify-content: center;
}

.brand-kicker {
    flex: 0 0 auto;

    margin:
        0
        0
        8px
        0;
}

.title {
    flex: 0 0 auto;

    margin: 0;
}

.subtitle {
    flex: 0 0 auto;

    margin-top: 12px;
}

.header-right {
    flex: 0 0 auto;
    align-self: center;
}

.profile-chip {
    min-width: 330px;

    padding:
        16px
        20px;
}


/* -------------------------------------------------------
   PROCESS AREA

   Separate subtitle/header from "검사 공정".
   ------------------------------------------------------- */

.process {
    padding:
        24px
        0
        24px;
}

.process-label {
    min-height: 48px;

    padding:
        0
        10px;

    margin-bottom: 8px;

    align-items: center;
}

.progress-track {
    margin:
        0
        0
        18px
        0;
}


/* -------------------------------------------------------
   FIVE VIEW CARDS

   More vertical room for:
   01 / View name / state / runtime+pose
   ------------------------------------------------------- */

.cards {
    gap: 14px;
}

.stage-card {
    min-height: 150px;

    padding:
        20px
        20px
        17px;

    justify-content: space-between;
}

.stage-top {
    min-height: 78px;
}

.step {
    margin-bottom: 9px;
}

.stage-name {
    margin-top: 0;
}

.state-pill {
    margin-top: 2px;

    padding:
        8px
        12px;
}

.stage-tech {
    margin-top: 16px;
}


/* -------------------------------------------------------
   MAIN WORKSPACE

   Give right inspector much more width.
   ------------------------------------------------------- */

.workspace {
    grid-template-columns:
        minmax(0, 1fr)
        520px;

    gap: 20px;
}


/* -------------------------------------------------------
   LIVE PANEL HEADER

   Left-side inspection title was too compressed.
   ------------------------------------------------------- */

.live-header {
    height: 92px;

    padding:
        0
        24px;
}

.live-title {
    min-width: 0;
}

.live-title > div {
    min-width: 0;
}

.live-title-main {
    margin-bottom: 9px;
}

.live-title-sub {
    margin-top: 0;
}

.connection {
    min-width: 190px;

    margin-left: 24px;

    justify-content: flex-end;
}


/* -------------------------------------------------------
   16:9 VIEWPORT

   Ratio remains untouched.
   Only preserve clean separation from header.
   ------------------------------------------------------- */

.viewport {
    margin: 0;
}


/* -------------------------------------------------------
   CENTER MESSAGE

   Give text more breathing room without changing font.
   ------------------------------------------------------- */

.offline {
    padding:
        60px
        90px;
}

.offline-icon {
    margin-bottom: 24px;
}

.offline-title {
    margin-bottom: 12px;
}

.offline-text {
    margin-top: 0;
    max-width: 820px;
}


/* -------------------------------------------------------
   RIGHT INSPECTOR
   ------------------------------------------------------- */

.inspector {
    gap: 18px;
}

.inspector-card {
    padding:
        22px
        22px
        21px;
}

.inspector-heading {
    min-height: 52px;

    margin-bottom: 16px;
}

.inspector-tag {
    padding:
        7px
        11px;
}


/* -------------------------------------------------------
   INSPECTION STATUS ROWS

   Large fixed row height = much clearer HMI alignment.
   ------------------------------------------------------- */

.info-row {
    grid-template-columns:
        minmax(0, 1fr)
        minmax(185px, auto);

    min-height: 68px;

    padding:
        10px
        4px;

    column-gap: 22px;
}

.info-label {
    padding-left: 2px;
}

.info-value {
    padding-right: 2px;
}


/* -------------------------------------------------------
   QUALITY SUMMARY

   More space inside each cell.
   ------------------------------------------------------- */

.quality-grid {
    gap: 12px;

    grid-auto-rows: 125px;
}

.q-box {
    padding:
        17px
        18px;
}

.q-label {
    margin-bottom: 12px;
}

.q-value {
    margin-top: 0;
}


/* -------------------------------------------------------
   FINAL PRE-ROOF RESULT
   ------------------------------------------------------- */

.final-box {
    min-height: 145px;

    margin-top: 14px !important;

    padding:
        22px
        20px;
}

.final-label {
    margin-bottom: 15px;
}

.final-value {
    margin-top: 0;
}


/* -------------------------------------------------------
   CONTROLS
   ------------------------------------------------------- */

.controls {
    gap: 12px;

    margin-top: 15px;
}

button {
    height: 58px;

    padding:
        0
        20px;
}


/* -------------------------------------------------------
   MESSAGE AREA

   Reserve height so buttons/system card do not jump.
   ------------------------------------------------------- */

.message {
    min-height: 44px;

    padding:
        12px
        4px
        2px;
}


/* -------------------------------------------------------
   SYSTEM INFORMATION

   Much more readable spacing.
   ------------------------------------------------------- */

.note {
    padding:
        4px
        2px
        6px;
}


/* -------------------------------------------------------
   FOOTER
   ------------------------------------------------------- */

.footer {
    min-height: 42px;

    padding:
        16px
        4px
        0;
}


/* -------------------------------------------------------
   LARGE-DISPLAY RESPONSIVE GUARD

   Still spacious, but inspector won't consume too much
   on narrower monitors.
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .workspace {
        grid-template-columns:
            minmax(0, 1fr)
            460px;
    }

    .header {
        height: 150px;
    }

    .stage-card {
        min-height: 138px;
    }

    .info-row {
        min-height: 62px;
    }

    .quality-grid {
        grid-auto-rows: 115px;
    }
}


/* -------------------------------------------------------
   STACK ONLY WHEN ACTUALLY NARROW
   ------------------------------------------------------- */

@media (max-width: 1350px) {

    .workspace {
        grid-template-columns: 1fr;
    }

    .inspector {
        display: grid;
        grid-template-columns:
            1fr
            1fr;
    }
}

</style>


<style>

/* =======================================================
   V12 FINAL ANCHOR / SPACING TUNE

   FONT SIZE      : UNCHANGED
   16:9 VIEWPORT  : UNCHANGED
   QC/API/STATE   : UNCHANGED
   ======================================================= */


/* -------------------------------------------------------
   TOP AREA - slightly more breathing room
   ------------------------------------------------------- */

.header {
    height: 175px;

    padding:
        22px
        20px
        20px
        18px;
}

.subtitle {
    margin-top: 15px;
}

.process {
    padding:
        28px
        0
        28px;
}

.process-label {
    min-height: 52px;

    padding:
        0
        12px;

    margin-bottom: 10px;
}

.progress-track {
    margin-bottom: 20px;
}


/* -------------------------------------------------------
   VIEW CARDS - slightly more vertical room
   ------------------------------------------------------- */

.cards {
    gap: 15px;
}

.stage-card {
    min-height: 158px;

    padding:
        21px
        20px
        18px;
}

.stage-top {
    min-height: 82px;
}

.stage-tech {
    margin-top: 18px;
}


/* -------------------------------------------------------
   WORKSPACE

   Critical:
   Right Inspector stretches to the exact same grid-row
   height as the left inspection panel.
   ------------------------------------------------------- */

.workspace {
    grid-template-columns:
        minmax(0, 1fr)
        540px;

    gap: 22px;

    align-items: stretch;
}


/* Left inspection panel keeps its own natural 16:9 size */

.live-panel {
    align-self: start;
}


/* -------------------------------------------------------
   LIVE HEADER
   ------------------------------------------------------- */

.live-header {
    height: 98px;

    padding:
        0
        26px;
}

.live-title-main {
    margin-bottom: 11px;
}

.connection {
    min-width: 210px;
    margin-left: 28px;
}


/* -------------------------------------------------------
   CENTER MESSAGE

   Prevent awkward line wrapping.
   ------------------------------------------------------- */

.offline {
    padding:
        70px
        110px;
}

.offline-icon {
    margin-bottom: 26px;
}

.offline-title {
    margin-bottom: 15px;
}

.offline-text {
    width: 100%;

    max-width: 1100px;

    margin-top: 0;

    line-height: 1.6;
}


/* -------------------------------------------------------
   RIGHT INSPECTOR

   Full-height flex column.
   System Information is anchored to bottom.
   ------------------------------------------------------- */

.inspector {
    align-self: stretch;

    height: 100%;

    display: flex;
    flex-direction: column;

    gap: 20px;
}


/* System Info = last card.
   Push it all the way to the bottom of the inspection area. */

.inspector >
.inspector-card:last-child {
    margin-top: auto;
}


/* -------------------------------------------------------
   INSPECTOR CARDS
   ------------------------------------------------------- */

.inspector-card {
    padding:
        24px
        23px
        23px;
}

.inspector-heading {
    min-height: 55px;
    margin-bottom: 18px;
}


/* -------------------------------------------------------
   STATUS ROWS
   ------------------------------------------------------- */

.info-row {
    min-height: 72px;

    grid-template-columns:
        minmax(0, 1fr)
        minmax(195px, auto);

    padding:
        11px
        5px;

    column-gap: 24px;
}


/* -------------------------------------------------------
   QUALITY SUMMARY
   ------------------------------------------------------- */

.quality-grid {
    gap: 13px;

    grid-auto-rows: 130px;
}

.q-box {
    padding:
        18px
        19px;
}

.q-label {
    margin-bottom: 14px;
}


/* -------------------------------------------------------
   FINAL RESULT
   ------------------------------------------------------- */

.final-box {
    min-height: 150px;

    margin-top: 16px !important;

    padding:
        24px
        22px;
}

.final-label {
    margin-bottom: 17px;
}


/* -------------------------------------------------------
   BUTTONS
   ------------------------------------------------------- */

.controls {
    margin-top: 17px;
    gap: 13px;
}

button {
    height: 60px;
}


/* -------------------------------------------------------
   MESSAGE
   ------------------------------------------------------- */

.message {
    min-height: 48px;

    padding:
        13px
        5px
        3px;
}


/* -------------------------------------------------------
   SYSTEM INFORMATION

   Slightly taller + internally centered.
   Bottom edge now aligns with left inspection panel.
   ------------------------------------------------------- */

.inspector >
.inspector-card:last-child {
    min-height: 165px;

    display: flex;
    flex-direction: column;
    justify-content: center;
}

.inspector >
.inspector-card:last-child
.inspector-heading {
    margin-bottom: 14px;
}

.inspector >
.inspector-card:last-child
.note {
    padding-bottom: 0;
}


/* -------------------------------------------------------
   FOOTER
   ------------------------------------------------------- */

.footer {
    min-height: 48px;

    padding:
        18px
        4px
        0;
}


/* -------------------------------------------------------
   MODERATE DISPLAY GUARD
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .workspace {
        grid-template-columns:
            minmax(0, 1fr)
            480px;

        gap: 18px;
    }

    .header {
        height: 160px;
    }

    .stage-card {
        min-height: 145px;
    }

    .info-row {
        min-height: 66px;
    }

    .quality-grid {
        grid-auto-rows: 118px;
    }

    .final-box {
        min-height: 138px;
    }

    .inspector >
    .inspector-card:last-child {
        min-height: 150px;
    }
}


/* -------------------------------------------------------
   NARROW DISPLAY
   ------------------------------------------------------- */

@media (max-width: 1350px) {

    .workspace {
        grid-template-columns: 1fr;
    }

    .inspector {
        height: auto;

        display: grid;

        grid-template-columns:
            1fr
            1fr;
    }

    .inspector >
    .inspector-card:last-child {
        margin-top: 0;
    }
}

</style>


<style>

/* =======================================================
   V13 SUMMARY / LIVE HEADER TUNE
   VISUAL ONLY

   FONT SIZE     : UNCHANGED
   16:9 VIEWPORT : UNCHANGED
   QC/API/STATE  : UNCHANGED
   ======================================================= */


/* -------------------------------------------------------
   LIVE INSPECTION HEADER

   Separate:
   상단 (TOP) 품질 검사
   V5 · VIEW_TOP_FIXED
   ------------------------------------------------------- */

.live-header {
    height: 122px;

    padding:
        0
        28px;
}

.live-title {
    height: 100%;

    display: flex;
    align-items: center;
}

.live-title > div {
    width: 100%;

    display: flex;
    flex-direction: column;
    justify-content: center;
}

.live-title-main {
    margin: 0 0 18px 0;

    line-height: 1;
}

.live-title-sub {
    margin: 0;

    line-height: 1;

    opacity: .92;
}

.connection {
    min-width: 220px;

    margin-left: 30px;

    align-self: center;

    justify-content: flex-end;
}


/* -------------------------------------------------------
   REMOVE SYSTEM INFO FROM PRESENTATION

   DOM/backend untouched.
   Presentation only.
   ------------------------------------------------------- */

.inspector >
.inspector-card:last-child {
    display: none !important;
}


/* -------------------------------------------------------
   RIGHT INSPECTOR

   Use remaining height for actual inspection information.
   ------------------------------------------------------- */

.inspector {
    height: 100%;

    display: flex;
    flex-direction: column;

    gap: 20px;
}


/* First card = 검사 현황 */

.inspector >
.inspector-card:first-child {
    flex:
        0
        0
        auto;
}


/* Second card = 품질 검사 요약 */

.inspector >
.inspector-card:nth-child(2) {
    flex:
        1
        1
        auto;

    min-height: 0;

    display: flex;
    flex-direction: column;

    padding:
        26px
        24px
        24px;
}


/* -------------------------------------------------------
   QUALITY SUMMARY HEADER
   ------------------------------------------------------- */

.inspector >
.inspector-card:nth-child(2)
.inspector-heading {
    min-height: 58px;

    margin-bottom: 20px;
}


/* -------------------------------------------------------
   2 x 2 QUALITY GRID
   ------------------------------------------------------- */

.quality-grid {
    grid-auto-rows: 145px;

    gap: 14px;
}

.q-box {
    padding:
        21px
        20px;

    justify-content: center;
}

.q-label {
    margin-bottom: 16px;
}

.q-value {
    margin-top: 0;
}


/* -------------------------------------------------------
   FINAL RESULT
   ------------------------------------------------------- */

.final-box {
    min-height: 165px;

    margin-top: 18px !important;

    padding:
        27px
        23px;
}

.final-label {
    margin-bottom: 20px;
}


/* -------------------------------------------------------
   CONTROLS
   ------------------------------------------------------- */

.controls {
    margin-top: 20px;

    gap: 14px;
}

button {
    height: 64px;
}


/* -------------------------------------------------------
   MESSAGE AREA

   Preserve status-message space without compressing
   the control section.
   ------------------------------------------------------- */

.message {
    min-height: 55px;

    padding:
        16px
        5px
        4px;
}


/* -------------------------------------------------------
   MODERATE DISPLAY GUARD
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .live-header {
        height: 112px;
    }

    .live-title-main {
        margin-bottom: 15px;
    }

    .quality-grid {
        grid-auto-rows: 132px;
    }

    .final-box {
        min-height: 150px;
    }

    button {
        height: 58px;
    }
}


/* -------------------------------------------------------
   NARROW SCREEN
   ------------------------------------------------------- */

@media (max-width: 1350px) {

    .inspector {
        height: auto;

        display: grid;

        grid-template-columns:
            1fr
            1fr;
    }

    .inspector >
    .inspector-card:nth-child(2) {
        min-height: auto;
    }
}

</style>


<style>

/* =======================================================
   V14 QUALITY SUMMARY FILL
   VISUAL ONLY
   ======================================================= */


/* -------------------------------------------------------
   QUALITY SUMMARY CARD FILLS REMAINING HEIGHT
   ------------------------------------------------------- */

.inspector >
.inspector-card:nth-child(2) {
    flex:
        1
        1
        auto;

    display: flex;
    flex-direction: column;

    min-height: 0;
}


/* Summary title stays at top */

.inspector >
.inspector-card:nth-child(2)
.inspector-heading {
    flex:
        0
        0
        auto;
}


/* -------------------------------------------------------
   QUALITY GRID
   ------------------------------------------------------- */

.quality-grid {
    flex:
        0
        0
        auto;

    grid-auto-rows: 150px;

    gap: 15px;
}


.q-box {
    display: flex;

    flex-direction: column;

    justify-content: center;

    padding:
        20px
        21px;
}


.q-label {
    margin: 0;

    line-height: 1;
}


.q-desc {
    margin-top: 9px;

    color: var(--muted2);

    font-size: 18px;

    line-height: 1.1;
}


.q-value {
    margin-top: 13px;
}


/* -------------------------------------------------------
   FINAL RESULT
   ------------------------------------------------------- */

.final-box {
    flex:
        0
        0
        auto;

    min-height: 175px;

    margin-top: 18px !important;

    padding:
        24px
        22px;
}


.final-label {
    margin: 0;
}


.final-desc {
    margin-top: 10px;

    color: var(--muted2);

    font-size: 18px;

    line-height: 1.15;
}


.final-value {
    margin-top: 18px;
}


/* -------------------------------------------------------
   PUSH CONTROL AREA TO BOTTOM

   This fills the lower empty part of the inspector card.
   ------------------------------------------------------- */

.controls {
    margin-top: auto;

    padding-top: 24px;

    gap: 14px;
}


/* Buttons visually substantial */

button {
    height: 66px;
}


/* -------------------------------------------------------
   MESSAGE AREA STAYS UNDER BUTTONS
   ------------------------------------------------------- */

.message {
    flex:
        0
        0
        auto;

    min-height: 58px;

    padding:
        15px
        5px
        4px;
}


/* -------------------------------------------------------
   MODERATE DISPLAY
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .quality-grid {
        grid-auto-rows: 138px;
    }

    .q-desc,
    .final-desc {
        font-size: 16px;
    }

    .final-box {
        min-height: 160px;
    }

    button {
        height: 60px;
    }
}

</style>


<style>

/* =======================================================
   V15 QUALITY SUMMARY FULL FILL
   VISUAL ONLY

   FONT SIZE     : UNCHANGED
   16:9 VIEWPORT : UNCHANGED
   QC/API/STATE  : UNCHANGED
   ======================================================= */


/* -------------------------------------------------------
   SUMMARY CARD = FULL HEIGHT COLUMN
   ------------------------------------------------------- */

.inspector >
.inspector-card:nth-child(2) {
    height: 100%;

    min-height: 0;

    display: flex;

    flex-direction: column;

    padding:
        26px
        24px
        24px;
}


/* -------------------------------------------------------
   HEADER
   ------------------------------------------------------- */

.inspector >
.inspector-card:nth-child(2)
.inspector-heading {
    flex:
        0
        0
        auto;

    min-height: 60px;

    margin-bottom: 20px;
}


/* -------------------------------------------------------
   FOUR SUMMARY CELLS

   Slightly taller than V14.
   ------------------------------------------------------- */

.quality-grid {
    flex:
        0
        0
        auto;

    grid-auto-rows: 165px;

    gap: 15px;
}


.q-box {
    display: flex;

    flex-direction: column;

    justify-content: center;

    padding:
        23px
        21px;
}


.q-label {
    margin: 0;

    line-height: 1;
}


.q-desc {
    margin-top: 10px;

    line-height: 1.15;
}


.q-value {
    margin-top: 15px;

    line-height: 1;
}


/* -------------------------------------------------------
   FINAL RESULT

   Main change:
   It grows and consumes the unused middle space.
   ------------------------------------------------------- */

.final-box {
    flex:
        1
        1
        auto;

    min-height: 210px;

    margin-top: 20px !important;

    padding:
        28px
        24px;

    display: flex;

    flex-direction: column;

    justify-content: center;
}


.final-label {
    margin: 0;

    line-height: 1;
}


.final-desc {
    margin-top: 12px;

    line-height: 1.15;
}


.final-value {
    margin-top: 24px;

    line-height: 1;
}


/* -------------------------------------------------------
   MESSAGE

   Status message sits ABOVE buttons.
   Empty message consumes no space.
   ------------------------------------------------------- */

.message {
    order: 4;

    flex:
        0
        0
        auto;

    min-height: 42px;

    padding:
        14px
        5px
        8px;
}


.message:empty {
    display: none;
}


/* -------------------------------------------------------
   BUTTON AREA

   Always at the very bottom.
   ------------------------------------------------------- */

.controls {
    order: 5;

    flex:
        0
        0
        auto;

    margin-top: 20px;

    padding-top: 0;

    padding-bottom: 0;

    gap: 14px;
}


button {
    height: 66px;
}


/* -------------------------------------------------------
   IMPORTANT:
   quality grid / final / message / controls ordering
   ------------------------------------------------------- */

.quality-grid {
    order: 1;
}

.final-box {
    order: 2;
}

.controls {
    order: 5;
}

.message {
    order: 4;
}


/* -------------------------------------------------------
   CURRENT DISPLAY SIZE GUARD

   Keep it spacious even below 2200px.
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .quality-grid {
        grid-auto-rows: 155px;
    }

    .final-box {
        min-height: 195px;
    }

    button {
        height: 62px;
    }
}


/* -------------------------------------------------------
   NARROW DISPLAY
   ------------------------------------------------------- */

@media (max-width: 1350px) {

    .inspector >
    .inspector-card:nth-child(2) {
        height: auto;
    }

    .final-box {
        flex:
            0
            0
            auto;
    }
}

</style>


<style>

/* =======================================================
   V16 QUALITY SUMMARY ALIGNMENT
   VISUAL ONLY
   ======================================================= */


/* -------------------------------------------------------
   COMPLETE / WAIT / PASS / FAIL
   Slightly taller
   ------------------------------------------------------- */

.quality-grid {
    grid-auto-rows: 180px;
    gap: 16px;
}

.q-box {
    padding:
        26px
        23px;

    justify-content: center;
}

.q-label {
    font-size: 30px !important;
}

.q-desc {
    margin-top: 11px;

    font-size: 20px !important;

    line-height: 1.15;
}

.q-value {
    margin-top: 16px;

    font-size: 54px !important;
}


/* -------------------------------------------------------
   FINAL RESULT BOX

   Title/description = TOP
   Result = CENTER
   ------------------------------------------------------- */

.final-box {
    position: relative;

    min-height: 230px;

    padding:
        26px
        24px
        20px;

    justify-content: flex-start;
}


/* Top fixed text */

.final-label {
    margin: 0;

    font-size: 30px !important;

    line-height: 1.1;
}

.final-desc {
    margin-top: 12px;

    font-size: 20px !important;

    line-height: 1.15;
}


/* Result centered in remaining body */

.final-value {
    position: absolute;

    left: 24px;
    right: 24px;

    top: 92px;
    bottom: 15px;

    margin: 0 !important;

    display: flex;

    align-items: center;
    justify-content: center;

    text-align: center;

    font-size: 72px !important;

    line-height: 1;
}


/* -------------------------------------------------------
   MODERATE DISPLAY / CURRENT 70% BROWSER SCALE
   ------------------------------------------------------- */

@media (max-width: 2200px) {

    .quality-grid {
        grid-auto-rows: 170px;
    }

    .q-label {
        font-size: 28px !important;
    }

    .q-desc {
        font-size: 19px !important;
    }

    .q-value {
        font-size: 52px !important;
    }

    .final-box {
        min-height: 215px;
    }

    .final-label {
        font-size: 28px !important;
    }

    .final-desc {
        font-size: 19px !important;
    }

    .final-value {
        top: 88px;

        font-size: 68px !important;
    }
}

</style>


<style>

/* =======================================================
   V17 FINAL UI TEXT / FUTURE HOLD VISUAL
   PRESENTATION ONLY
   ======================================================= */

.stage-card.HOLD {
    border-color: #b9782f;
}

.state-pill.HOLD {
    background: rgba(224, 148, 63, .14);
    color: #f0ae5d;
}

.HOLD {
    color: #f0ae5d;
}

</style>

</head>


<body>

<div class="app">


<!-- ======================================================
     HEADER
     ====================================================== -->

<header class="header">

    <div>

        <div class="brand-kicker">
            HARMONY VISION INSPECTION
        </div>

        <div class="title">
            PRE-ROOF 조립 품질 검사
        </div>

        <div class="subtitle">
            5방향 통합 비전 검사 시스템
        </div>

    </div>


    <div class="header-right">

        <div class="profile-chip">

            <div class="a">
                최종 검사 프로필
            </div>

            <div class="b">
                검증된 검사 설정 적용
            </div>

        </div>

    </div>

</header>



<!-- ======================================================
     PROCESS
     ====================================================== -->

<section class="process">

    <div class="process-label">

        <div class="process-title">
            검사 공정
        </div>

        <div
            id="progressText"
            class="progress-text">
            0 / 5 완료
        </div>

    </div>

    <div class="progress-track">

        <div
            id="progressBar"
            class="progress-bar">
        </div>

    </div>

    <div
        id="cards"
        class="cards">
    </div>

</section>



<!-- ======================================================
     WORKSPACE
     ====================================================== -->

<section class="workspace">


    <!-- LIVE -->

    <div class="live-panel">

        <div class="live-header">

            <div class="live-title">

                <div>

                    <div
                        id="liveName"
                        class="live-title-main">
                        검사 화면 대기
                    </div>

                    <div
                        id="liveTech"
                        class="live-title-sub">
                        검사 위치가 활성화되면
                        해당 View 화면을 표시합니다.
                    </div>

                </div>

            </div>


            <div class="connection">

                <span
                    id="connectionDot"
                    class="dot">
                </span>

                <span id="connectionText">
                    검사 대기
                </span>

            </div>

        </div>


        <div class="viewport">

            <iframe
                id="frame"
                src="about:blank">
            </iframe>


            <div
                id="offline"
                class="offline">

                <div class="offline-icon">
                    ◇
                </div>

                <div
                    id="offlineTitle"
                    class="offline-title">
                    검사 위치 선택 대기 중
                </div>

                <div
                    id="offlineText"
                    class="offline-text">
                    검사 공정이 시작되면 현재 활성화된
                    위치의 최종 품질 검사 화면이 표시됩니다.
                </div>

            </div>

        </div>

    </div>



    <!-- INSPECTOR -->

    <aside class="inspector">


        <div class="inspector-card">

            <div class="inspector-heading">

                <span>
                    검사 현황
                </span>

                <span class="inspector-tag">
                    INSPECTOR
                </span>

            </div>


            <div class="info-row">

                <span class="info-label">
                    현재 검사 위치
                </span>

                <span
                    id="current"
                    class="info-value">
                    없음
                </span>

            </div>


            <div class="info-row">

                <span class="info-label">
                    검사 모듈
                </span>

                <span
                    id="runtime"
                    class="info-value">
                    -
                </span>

            </div>


            <div class="info-row">

                <span class="info-label">
                    검사 상태
                </span>

                <span
                    id="health"
                    class="info-value">
                    대기
                </span>

            </div>


            <div class="info-row">

                <span class="info-label">
                    다음 검사 위치
                </span>

                <span
                    id="next"
                    class="info-value">
                    상단 (TOP)
                </span>

            </div>

        </div>



        <div class="inspector-card">

            <div class="inspector-heading">

                <span>
                    품질 검사 요약
                </span>

                <span class="inspector-tag">
                    LIVE
                </span>

            </div>


            <div class="quality-grid">

                <div class="q-box">

                    <div class="q-label">
                        완료
                    </div>

                    <div class="q-desc">
                        결과 확정 완료
                    </div>

                    <div
                        id="doneCount"
                        class="q-value">
                        0 / 5
                    </div>

                </div>


                <div class="q-box">

                    <div class="q-label">
                        대기
                    </div>

                    <div class="q-desc">
                        검사 시작 전
                    </div>

                    <div
                        id="waitingCount"
                        class="q-value wait">
                        5
                    </div>

                </div>


                <div class="q-box">

                    <div class="q-label">
                        정상
                    </div>

                    <div class="q-desc">
                        PASS 확정
                    </div>

                    <div
                        id="passCount"
                        class="q-value pass">
                        0
                    </div>

                </div>


                <div class="q-box">

                    <div class="q-label">
                        이상
                    </div>

                    <div class="q-desc">
                        FAIL 확정
                    </div>

                    <div
                        id="failCount"
                        class="q-value fail">
                        0
                    </div>

                </div>

            </div>


            <div
                class="final-box"
                style="margin-top:9px;">

                <div class="final-label">
                    PRE-ROOF 최종 판정
                </div>

                <div class="final-desc">
                    5방향 검사 결과 종합
                </div>

                <div
                    id="final"
                    class="final-value WAITING">
                    대기
                </div>

            </div>


            <div class="controls">

                <button
                    class="primary"
                    onclick="activateNext()">
                    다음 위치 검사 시작
                </button>

                <button
                    onclick="commitCurrent()">
                    현재 검사 결과 확정
                </button>

                <button
                    onclick="resetSequence()">
                    검사 순서 초기화
                </button>

            </div>


            <div
                id="msg"
                class="message">
            </div>

        </div>



        <div class="inspector-card">

            <div class="inspector-heading">

                <span>
                    시스템 정보
                </span>

            </div>

            <div class="note">
                본 화면은 로봇을 직접 제어하지 않습니다.
                검사 위치 전환은 검사 상태 신호만 처리하며,
                각 방향의 품질 판정은 기존 FINAL_ACTIVE
                검사 모듈에서 수행합니다.
            </div>

        </div>


    </aside>

</section>



<footer class="footer">

    <span>
        HARMONY · PRE-ROOF QUALITY INSPECTION
    </span>

    <span>
        5 VIEW · RGB + DEPTH SHAPE
    </span>

</footer>


</div>



<script>

const VIEW_LABEL = {
    TOP: '상단',
    LEFT: '좌측',
    RIGHT: '우측',
    FRONT: '전면',
    BEHIND: '후면'
};

const STATE_LABEL = {
    WAITING: '대기',
    ACTIVE: '검사 중',
    PASS: '정상',
    FAIL: '이상',
    HOLD: '교체 대기'
};

let lastFrameUrl = null;


function viewLabel(view) {

    if (!view) {
        return '없음';
    }

    return (
        (VIEW_LABEL[view] || view) +
        ' (' + view + ')'
    );
}


function stateLabel(state) {

    if (!state) {
        return '-';
    }

    return (
        STATE_LABEL[state]
        || state
    );
}


async function post(
    path,
    body={}
) {

    const r = await fetch(
        path,
        {
            method: 'POST',

            headers: {
                'Content-Type':
                    'application/json'
            },

            body:
                JSON.stringify(body)
        }
    );

    const d =
        await r.json();

    document.getElementById(
        'msg'
    ).textContent =
        d.message || '';

    return d;
}


async function activate(view) {

    await post(
        '/api/activate',
        {view}
    );

    await refresh();
}


async function activateNext() {

    await post(
        '/api/activate-next'
    );

    await refresh();
}


async function commitCurrent() {

    await post(
        '/api/commit'
    );

    await refresh();
}


async function resetSequence() {

    await post(
        '/api/reset'
    );

    await refresh();
}


function runtimeFrameUrl(port) {

    return (
        window.location.protocol +
        '//' +
        window.location.hostname +
        ':' +
        port +
        '/'
    );
}


function updateQualitySummary(d) {

    const states =
        Object.values(
            d.views
        );

    const pass =
        states.filter(
            x => x === 'PASS'
        ).length;

    const fail =
        states.filter(
            x => x === 'FAIL'
        ).length;

    const active =
        states.filter(
            x => x === 'ACTIVE'
        ).length;

    const waiting =
        states.filter(
            x => x === 'WAITING'
        ).length;

    const done =
        pass + fail;

    document.getElementById(
        'doneCount'
    ).textContent =
        done + ' / 5';

    document.getElementById(
        'passCount'
    ).textContent =
        pass;

    document.getElementById(
        'failCount'
    ).textContent =
        fail;

    document.getElementById(
        'waitingCount'
    ).textContent =
        waiting;

    document.getElementById(
        'progressText'
    ).textContent =
        done + ' / 5 완료';

    document.getElementById(
        'progressBar'
    ).style.width =
        (
            (done / 5) * 100
        ) + '%';
}


async function refresh() {

    try {

        const r =
            await fetch(
                '/status',
                {
                    cache:
                        'no-store'
                }
            );

        const d =
            await r.json();


        /* -----------------------------------------------
           PROCESS CARDS
           ----------------------------------------------- */

        const cards =
            document.getElementById(
                'cards'
            );

        cards.innerHTML = '';


        d.sequence.forEach(
            (view, index) => {

                const state =
                    d.views[view];

                const p =
                    d.profiles[view];

                const card =
                    document.createElement(
                        'div'
                    );

                card.className =
                    'stage-card ' +
                    state;

                card.onclick =
                    () => activate(
                        view
                    );

                card.innerHTML =
                    '<div class="stage-top">' +

                        '<div>' +

                            '<div class="step">' +
                                String(
                                    index + 1
                                ).padStart(
                                    2,
                                    '0'
                                ) +
                            '</div>' +

                            '<div class="stage-name">' +
                                viewLabel(view) +
                            '</div>' +

                        '</div>' +

                        '<div class="state-pill ' +
                            state +
                        '">' +
                            stateLabel(state) +
                        '</div>' +

                    '</div>' +

                    '<div class="stage-tech">' +
                        p.runtime_version +
                        ' · ' +
                        p.robot_pose +
                    '</div>';

                cards.appendChild(
                    card
                );
            }
        );


        /* -----------------------------------------------
           SUMMARY
           ----------------------------------------------- */

        updateQualitySummary(
            d
        );


        const current =
            d.current_view;


        document.getElementById(
            'current'
        ).textContent =
            viewLabel(
                current
            );


        document.getElementById(
            'next'
        ).textContent =
            viewLabel(
                d.expected_next
            );


        const finalEl =
            document.getElementById(
                'final'
            );

        finalEl.textContent =
            stateLabel(
                d.final_result
            );

        finalEl.className =
            'final-value ' +
            d.final_result;



        const frame =
            document.getElementById(
                'frame'
            );

        const offline =
            document.getElementById(
                'offline'
            );

        const offlineTitle =
            document.getElementById(
                'offlineTitle'
            );

        const offlineText =
            document.getElementById(
                'offlineText'
            );

        const dot =
            document.getElementById(
                'connectionDot'
            );

        const connectionText =
            document.getElementById(
                'connectionText'
            );


        /* -----------------------------------------------
           NO ACTIVE VIEW
           ----------------------------------------------- */

        if (!current) {

            document.getElementById(
                'runtime'
            ).textContent =
                '-';

            document.getElementById(
                'health'
            ).textContent =
                '대기';

            document.getElementById(
                'liveName'
            ).textContent =
                '검사 화면 대기';

            document.getElementById(
                'liveTech'
            ).textContent =
                '검사 위치가 활성화되면 해당 View 화면을 표시합니다.';

            dot.className =
                'dot';

            connectionText.textContent =
                '검사 대기';

            offline.style.display =
                'flex';

            offlineTitle.textContent =
                '검사 위치 선택 대기 중';

            offlineText.textContent =
                '검사 공정이 시작되면 현재 활성화된 위치의 최종 품질 검사 화면이 표시됩니다.';

            if (
                lastFrameUrl !== null
            ) {

                frame.src =
                    'about:blank';

                lastFrameUrl =
                    null;
            }

            return;
        }


        /* -----------------------------------------------
           ACTIVE VIEW
           ----------------------------------------------- */

        const profile =
            d.profiles[current];

        const health =
            d.active_runtime;


        document.getElementById(
            'runtime'
        ).textContent =
            profile.runtime_version +
            ' / ' +
            profile.runtime_port;


        document.getElementById(
            'liveName'
        ).textContent =
            viewLabel(current) +
            ' 품질 검사';


        document.getElementById(
            'liveTech'
        ).textContent =
            profile.runtime_version +
            ' · ' +
            profile.robot_pose;



        /* -----------------------------------------------
           ONLINE
           ----------------------------------------------- */

        if (
            health &&
            health.online
        ) {

            document.getElementById(
                'health'
            ).textContent =
                '실행 중 / ' +
                stateLabel(
                    health.result
                    || 'UNKNOWN'
                );


            dot.className =
                'dot online';

            connectionText.textContent =
                '검사 모듈 연결';


            const url =
                runtimeFrameUrl(
                    profile.runtime_port
                );


            if (
                lastFrameUrl !== url
            ) {

                frame.src =
                    url;

                lastFrameUrl =
                    url;
            }


            offline.style.display =
                'none';

        }


        /* -----------------------------------------------
           OFFLINE
           ----------------------------------------------- */

        else {

            document.getElementById(
                'health'
            ).textContent =
                '미실행';


            dot.className =
                'dot offline';

            connectionText.textContent =
                '검사 모듈 미연결';


            offline.style.display =
                'flex';

            offlineTitle.textContent =
                viewLabel(current) +
                ' 검사 중';


            offlineText.innerHTML =
                '해당 검사 모듈이 현재 실행되지 않았습니다.<br>' +
                '검사 모듈이 연결되면 이 영역에 실제 품질 검사 화면이 표시됩니다.';


            if (
                lastFrameUrl !== null
            ) {

                frame.src =
                    'about:blank';

                lastFrameUrl =
                    null;
            }
        }

    }


    catch (e) {

        document.getElementById(
            'msg'
        ).textContent =
            '상태 조회 오류: ' +
            e;
    }
}


refresh();

setInterval(
    refresh,
    700
);

</script>

</body>
</html>
"""
# =========================================================
# HTTP
# =========================================================

class Handler(
    BaseHTTPRequestHandler
):

    def send_common_headers(
        self,
        code,
        content_type,
        length,
    ):

        self.send_response(
            code
        )

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(length)
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()


    def send_json(
        self,
        obj,
        code=200,
    ):

        raw = json.dumps(
            obj,
            ensure_ascii=False,
            indent=2
        ).encode(
            "utf-8"
        )

        self.send_common_headers(
            code,
            "application/json; charset=utf-8",
            len(raw),
        )

        self.wfile.write(
            raw
        )


    def read_json(self):

        n = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if n <= 0:
            return {}

        raw = self.rfile.read(
            n
        )

        if not raw:
            return {}

        return json.loads(
            raw.decode(
                "utf-8"
            )
        )


    def do_GET(self):

        path = urlparse(
            self.path
        ).path

        if path == "/":

            raw = PAGE.encode(
                "utf-8"
            )

            self.send_common_headers(
                200,
                "text/html; charset=utf-8",
                len(raw),
            )

            self.wfile.write(
                raw
            )

            return


        if path in (
            "/status",
            "/api/state",
        ):

            self.send_json(
                integrated_status()
            )

            return


        self.send_json(
            {
                "error":
                    "요청한 화면을 찾을 수 없습니다."
            },
            404
        )


    def do_POST(self):

        path = urlparse(
            self.path
        ).path

        try:

            body = self.read_json()

        except Exception as e:

            self.send_json(
                {
                    "ok":
                        False,

                    "message":
                        f"요청 데이터 형식이 올바르지 않습니다.",
                },
                400
            )

            return


        if path == "/api/activate":

            ok, message = (
                activate_view(
                    str(
                        body.get(
                            "view",
                            ""
                        )
                    ).upper()
                )
            )


        elif path == "/api/activate-next":

            ok, message = (
                activate_next()
            )


        elif path == "/api/commit":

            ok, message = (
                commit_current()
            )


        elif path == "/api/reset":

            ok, message = (
                reset_sequence()
            )


        else:

            self.send_json(
                {
                    "ok":
                        False,

                    "message":
                        "지원하지 않는 요청입니다.",
                },
                404
            )

            return


        self.send_json(
            {
                "ok":
                    ok,

                "message":
                    message,

                "status":
                    integrated_status(),
            },
            200 if ok else 409
        )


    def log_message(
        self,
        format,
        *args
    ):

        return


# =========================================================
# START
# =========================================================

print()
print("============================================================")
print(" HARMONY PRE-ROOF 5-VIEW DASHBOARD V17 / UI FINAL CANDIDATE")
print("============================================================")

for view in ORDER:

    p = PROFILES[view]

    print(
        f"{view:7s}: "
        f"{p['runtime_version']} / "
        f"{p['runtime_port']} / "
        f"{p['robot_pose']}"
    )

print()
print(
    f"DASHBOARD : "
    f"http://127.0.0.1:{PORT}/"
)

print(
    f"STATUS    : "
    f"http://127.0.0.1:{PORT}/status"
)

print(
    "ROBOT MOVE: DISABLED"
)

print(
    "CAMERA    : NOT REQUIRED FOR DASHBOARD START"
)

print(
    "production_valid=false"
)

print("============================================================")
print()

server = ThreadingHTTPServer(
    (
        "0.0.0.0",
        PORT,
    ),
    Handler,
)

try:

    server.serve_forever()

except KeyboardInterrupt:

    pass

finally:

    server.server_close()

    print()
    print(
        "5-VIEW dashboard stopped"
    )
