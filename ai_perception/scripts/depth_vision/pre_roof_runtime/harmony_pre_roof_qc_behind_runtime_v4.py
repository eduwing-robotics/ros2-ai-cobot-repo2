#!/usr/bin/env python3

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread, Lock
import urllib.request
import json
import time
import traceback

import cv2
import numpy as np


ROOT = Path.home() / "vision_project"

BASE = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc"
)

PORT = 8792

RGB_URL = "http://192.168.20.10:8766/raw"
DEPTH_URL = "http://192.168.20.10:8766/depthimg"

ROI_PATH = (
    BASE /
    "view_behind_roi_v3.json"
)

THRESHOLD_PATH = (
    BASE /
    "view_behind_center_structure_thresholds_v1.json"
)

GOLDEN_RGB_PATH = (
    BASE /
    "view_behind_golden_v1/golden_rgb_median.png"
)

GOLDEN_DEPTH_PATH = (
    BASE /
    "view_behind_golden_v1/golden_depth_median.png"
)


roi_doc = json.loads(
    ROI_PATH.read_text(
        encoding="utf-8"
    )
)

threshold_doc = json.loads(
    THRESHOLD_PATH.read_text(
        encoding="utf-8"
    )
)

ROI_MAP = {
    r["name"]: r
    for r in roi_doc["rois"]
}

CENTER = ROI_MAP["CENTER_STRUCTURE"]

TH = threshold_doc["thresholds"]

golden_rgb = cv2.imread(
    str(GOLDEN_RGB_PATH),
    cv2.IMREAD_COLOR
)

golden_depth = cv2.imread(
    str(GOLDEN_DEPTH_PATH),
    cv2.IMREAD_COLOR
)

if golden_rgb is None:
    raise SystemExit(
        f"Missing Golden RGB: {GOLDEN_RGB_PATH}"
    )

if golden_depth is None:
    raise SystemExit(
        f"Missing Golden Depth: {GOLDEN_DEPTH_PATH}"
    )

if golden_rgb.shape[:2] != (720, 1280):
    raise SystemExit(
        f"Unexpected Golden RGB size: {golden_rgb.shape}"
    )

if golden_depth.shape[:2] != (720, 1280):
    raise SystemExit(
        f"Unexpected Golden Depth size: {golden_depth.shape}"
    )


cx = int(CENTER["x"])
cy = int(CENTER["y"])
cw = int(CENTER["w"])
ch = int(CENTER["h"])

ref_rgb = golden_rgb[
    cy:cy+ch,
    cx:cx+cw
]

ref_depth = golden_depth[
    cy:cy+ch,
    cx:cx+cw
]


state_lock = Lock()

latest_status = {
    "schema":
        "harmony_pre_roof_view_behind_runtime_v4",

    "inspection":
        "PRE_ROOF",

    "view":
        "BEHIND",

    "runtime":
        "V4",

    "runtime_port":
        PORT,

    "result":
        "NOT_EVALUATED",

    "robot_pose":
        "VIEW_BEHIND_FIXED",

    "transform":
        "IDENTITY",

    "golden":
        "V1",

    "roi":
        "V3",

    "structural_threshold":
        "CENTER_STRUCTURE_V1",

    "primary_roi":
        "CENTER_STRUCTURE",

    "decision_policy":
        "NORMAL_REFERENCE_DEVIATION",

    "depth_policy":
        "DIAGNOSTIC_ONLY",

    "secondary_roi_policy":
        "COMMON_QC_PRESENTATION",

    "production_valid":
        False,

    "center_structure":
        {},

    "error":
        None,
}

latest_dashboard_jpeg = None


def fetch_image(url):

    with urllib.request.urlopen(
        url,
        timeout=5
    ) as r:
        data = r.read()

    img = cv2.imdecode(
        np.frombuffer(
            data,
            dtype=np.uint8
        ),
        cv2.IMREAD_COLOR
    )

    if img is None:
        raise RuntimeError(
            f"decode failed: {url}"
        )

    if img.shape[:2] != (720, 1280):
        raise RuntimeError(
            f"unexpected image size: {img.shape}"
        )

    return img


def normalize_gray(img):

    gray = cv2.cvtColor(
        img,
        cv2.COLOR_BGR2GRAY
    )

    return cv2.equalizeHist(
        gray
    )


REF_NORM = normalize_gray(
    ref_rgb
)


def structure_metrics(cur_rgb):

    cur_norm = normalize_gray(
        cur_rgb
    )

    diff = cv2.absdiff(
        cur_norm,
        REF_NORM
    )

    correlation = float(
        cv2.matchTemplate(
            cur_norm,
            REF_NORM,
            cv2.TM_CCOEFF_NORMED
        )[0, 0]
    )

    return {
        "normalized_mean_absdiff":
            float(
                diff.mean()
            ),

        "normalized_p95_absdiff":
            float(
                np.percentile(
                    diff,
                    95
                )
            ),

        "template_correlation":
            correlation,
    }


def depth_diagnostics(cur_depth):

    diff = cv2.absdiff(
        cur_depth,
        ref_depth
    )

    gray = cv2.cvtColor(
        diff,
        cv2.COLOR_BGR2GRAY
    )

    return {
        "mean_absdiff":
            float(
                gray.mean()
            ),

        "p95_absdiff":
            float(
                np.percentile(
                    gray,
                    95
                )
            ),

        "changed_fraction_20":
            float(
                np.mean(
                    gray >= 20
                )
            ),
    }


def evaluate_structure(m):

    exceeded = {
        "normalized_mean_absdiff":
            (
                m["normalized_mean_absdiff"]
                >
                float(
                    TH[
                        "normalized_mean_absdiff"
                    ]
                )
            ),

        "normalized_p95_absdiff":
            (
                m["normalized_p95_absdiff"]
                >
                float(
                    TH[
                        "normalized_p95_absdiff"
                    ]
                )
            ),

        "template_correlation":
            (
                m["template_correlation"]
                <
                float(
                    TH[
                        "template_correlation"
                    ]
                )
            ),
    }

    count = sum(
        1
        for v in exceeded.values()
        if v
    )

    result = (
        "FAIL"
        if count >= 2
        else "PASS"
    )

    reason = (
        "STRUCTURAL_REFERENCE_DEVIATION"
        if result == "FAIL"
        else "WITHIN_STRUCTURAL_REFERENCE"
    )

    return {
        "result":
            result,

        "reason":
            reason,

        "exceed_count":
            count,

        "required_exceed_count":
            2,

        "metrics":
            m,

        "thresholds":
            {
                "normalized_mean_absdiff":
                    float(
                        TH[
                            "normalized_mean_absdiff"
                        ]
                    ),

                "normalized_p95_absdiff":
                    float(
                        TH[
                            "normalized_p95_absdiff"
                        ]
                    ),

                "template_correlation":
                    float(
                        TH[
                            "template_correlation"
                        ]
                    ),
            },

        "exceeded":
            exceeded,
    }


def draw_dashboard(
    rgb,
    depth,
    status
):

    canvas = np.zeros(
        (720, 1280, 3),
        dtype=np.uint8
    )

    rgb_overlay = rgb.copy()

    center_result = (
        status[
            "center_structure"
        ]["result"]
    )

    result_color = (
        (0, 255, 0)
        if center_result == "PASS"
        else (0, 0, 255)
    )

    # -----------------------------------------------------
    # Presentation ROI = same family as FRONT.
    # CENTER_STRUCTURE remains hidden internal sub-ROI.
    # -----------------------------------------------------

    presentation_rois = (
        "BEHIND_BODY",
        "UPPER_REGION",
        "MIDDLE_REGION",
        "LOWER_REGION",
    )

    for name in presentation_rois:

        r = ROI_MAP[name]

        x = int(r["x"])
        y = int(r["y"])
        w = int(r["w"])
        h = int(r["h"])

        # All presentation ROIs follow the final view state
        # visually. Actual final decision remains CENTER_STRUCTURE.
        cv2.rectangle(
            rgb_overlay,
            (x, y),
            (x + w, y + h),
            result_color,
            4 if name == "BEHIND_BODY" else 2
        )

        cv2.putText(
            rgb_overlay,
            name,
            (x + 6, y + 21),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            result_color,
            2,
            cv2.LINE_AA
        )


    def txt(
        text,
        x,
        y,
        scale=0.55,
        color=(165, 165, 165),
        thickness=1,
    ):

        cv2.putText(
            canvas,
            text,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            thickness,
            cv2.LINE_AA
        )


    # Header
    txt(
        "HARMONY PRE-ROOF QUALITY INSPECTION",
        42,
        48,
        0.88,
        (240, 240, 240),
        2
    )

    txt(
        "VIEW_BEHIND / IDENTITY",
        42,
        77,
        0.50,
        (150, 150, 150),
        1
    )

    cv2.putText(
        canvas,
        f"VIEW_BEHIND : {status['result']}",
        (855, 52),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.83,
        result_color,
        2,
        cv2.LINE_AA
    )


    txt(
        "RGB QC + ROI / IDENTITY",
        42,
        111,
        0.48,
        (165, 165, 165),
        1
    )

    txt(
        "DEPTH SHAPE / IDENTITY",
        790,
        111,
        0.48,
        (165, 165, 165),
        1
    )


    rgb_panel = cv2.resize(
        rgb_overlay,
        (700, 394)
    )

    canvas[
        125:519,
        42:742
    ] = rgb_panel


    depth_panel = cv2.resize(
        depth,
        (435, 245)
    )

    canvas[
        125:370,
        790:1225
    ] = depth_panel


    # Right-side presentation = same 4 ROI structure.
    y = 407

    for name in presentation_rois:

        txt(
            name,
            800,
            y,
            0.50,
            (170, 170, 170),
            1
        )

        # Common PRE-ROOF QC presentation.
        # All visible regions use the same PASS/FAIL semantics
        # as the other fixed views.
        #
        # CENTER_STRUCTURE remains an internal view-profile
        # feature used to obtain robust BEHIND structural
        # evidence; it is not a separate quality standard.
        display_result = status["result"]
        display_color = result_color

        cv2.putText(
            canvas,
            display_result,
            (1115, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.51,
            display_color,
            2,
            cv2.LINE_AA
        )

        y += 32


    txt(
        "ROBOT POSE",
        55,
        566,
        0.46,
        (145, 145, 145),
        1
    )

    txt(
        "VIEW_BEHIND / FIXED",
        205,
        566,
        0.58,
        (235, 235, 235),
        2
    )


    txt(
        "QC INPUT",
        55,
        606,
        0.46,
        (145, 145, 145),
        1
    )

    txt(
        "D435 RGB + DEPTH SHAPE",
        205,
        606,
        0.58,
        (235, 235, 235),
        2
    )


    txt(
        "BEHIND RESULT",
        55,
        646,
        0.46,
        (145, 145, 145),
        1
    )

    cv2.putText(
        canvas,
        status["result"],
        (205, 646),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        result_color,
        2,
        cv2.LINE_AA
    )


    txt(
        "DEVELOPMENT / production_valid=false",
        790,
        646,
        0.43,
        (145, 145, 145),
        1
    )

    return canvas


def worker():

    global latest_status
    global latest_dashboard_jpeg

    while True:

        try:

            rgb = fetch_image(
                RGB_URL
            )

            depth = fetch_image(
                DEPTH_URL
            )

            cur_rgb = rgb[
                cy:cy+ch,
                cx:cx+cw
            ]

            cur_depth = depth[
                cy:cy+ch,
                cx:cx+cw
            ]

            structure = (
                structure_metrics(
                    cur_rgb
                )
            )

            evaluation = (
                evaluate_structure(
                    structure
                )
            )

            depth_diag = (
                depth_diagnostics(
                    cur_depth
                )
            )

            view_result = (
                evaluation["result"]
            )

            status = {
                "schema":
                    "harmony_pre_roof_view_behind_runtime_v4",

                "inspection":
                    "PRE_ROOF",

                "view":
                    "BEHIND",

                "runtime":
                    "V4",

                "runtime_port":
                    PORT,

                "result":
                    view_result,

                "robot_pose":
                    "VIEW_BEHIND_FIXED",

                "transform":
                    "IDENTITY",

                "golden":
                    "V1",

                "roi":
                    "V3",

                "structural_threshold":
                    "CENTER_STRUCTURE_V1",

                "primary_roi":
                    "CENTER_STRUCTURE",

                "decision_policy":
                    "NORMAL_REFERENCE_DEVIATION",

                "depth_policy":
                    "DIAGNOSTIC_ONLY",

                "secondary_roi_policy":
                    "COMMON_QC_PRESENTATION",

                "center_structure": {
                    **evaluation,

                    "depth_diagnostic":
                        depth_diag,
                },

                "production_valid":
                    False,

                "error":
                    None,

                "timestamp":
                    time.time(),
            }

            dashboard = draw_dashboard(
                rgb,
                depth,
                status
            )

            ok, buf = cv2.imencode(
                ".jpg",
                dashboard,
                [
                    cv2.IMWRITE_JPEG_QUALITY,
                    90
                ]
            )

            if not ok:
                raise RuntimeError(
                    "dashboard JPEG encode failed"
                )

            with state_lock:

                latest_status = status

                latest_dashboard_jpeg = (
                    buf.tobytes()
                )


        except Exception as e:

            traceback.print_exc()

            with state_lock:

                latest_status = {
                    "schema":
                        "harmony_pre_roof_view_behind_runtime_v4",

                    "inspection":
                        "PRE_ROOF",

                    "view":
                        "BEHIND",

                    "runtime":
                        "V4",

                    "runtime_port":
                        PORT,

                    "result":
                        "ERROR",

                    "robot_pose":
                        "VIEW_BEHIND_FIXED",

                    "transform":
                        "IDENTITY",

                    "golden":
                        "V1",

                    "roi":
                        "V3",

                    "structural_threshold":
                        "CENTER_STRUCTURE_V1",

                    "production_valid":
                        False,

                    "error":
                        repr(e),
                }

            time.sleep(0.5)

        time.sleep(0.12)


HTML = b"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Harmony PRE-ROOF BEHIND</title>
<style>
html, body {
    margin: 0;
    background: #202020;
    width: 100%;
    height: 100%;
}
.wrap {
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: 20px;
}
img {
    width: min(1280px, calc(100vw - 20px));
    height: auto;
}
</style>
</head>
<body>
<div class="wrap">
<img src="/stream">
</div>
</body>
</html>
"""


class Handler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):
        return


    def do_GET(self):

        if self.path == "/":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(HTML))
            )

            self.end_headers()

            self.wfile.write(
                HTML
            )

            return


        if self.path == "/status":

            with state_lock:

                data = json.dumps(
                    latest_status,
                    indent=2,
                    ensure_ascii=False
                ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(data))
            )

            self.end_headers()

            self.wfile.write(
                data
            )

            return


        if self.path == "/raw":

            with state_lock:
                data = latest_dashboard_jpeg

            if data is None:
                self.send_error(
                    503,
                    "No frame yet"
                )
                return

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "image/jpeg"
            )

            self.send_header(
                "Content-Length",
                str(len(data))
            )

            self.end_headers()

            self.wfile.write(
                data
            )

            return


        if self.path == "/stream":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame"
            )

            self.send_header(
                "Cache-Control",
                "no-cache"
            )

            self.end_headers()

            try:

                while True:

                    with state_lock:
                        data = latest_dashboard_jpeg

                    if data is None:
                        time.sleep(0.1)
                        continue

                    self.wfile.write(
                        b"--frame\r\n"
                    )

                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\n"
                    )

                    self.wfile.write(
                        f"Content-Length: {len(data)}\r\n\r\n"
                        .encode("ascii")
                    )

                    self.wfile.write(
                        data
                    )

                    self.wfile.write(
                        b"\r\n"
                    )

                    time.sleep(0.15)

            except (
                BrokenPipeError,
                ConnectionResetError,
            ):
                pass

            return


        self.send_error(404)


Thread(
    target=worker,
    daemon=True
).start()


print()
print("============================================================")
print(" HARMONY PRE-ROOF VIEW_BEHIND QC RUNTIME V4")
print("============================================================")
print("PORT       :", PORT)
print("POSE       : VIEW_BEHIND_FIXED")
print("TRANSFORM  : IDENTITY")
print("GOLDEN     : V1")
print("ROI        : V3")
print("PRIMARY    : CENTER_STRUCTURE")
print("STRUCT TH  : V1")
print("DEPTH      : DIAGNOSTIC_ONLY")
print("PRESENTATION: COMMON PRE-ROOF QC")
print("DASHBOARD  : http://127.0.0.1:8792/")
print("STATUS     : http://127.0.0.1:8792/status")
print("production_valid=false")
print()

ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    Handler
).serve_forever()
