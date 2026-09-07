#!/usr/bin/env python3

from pathlib import Path
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np


ROOT = Path.home() / "vision_project"

SOURCE = "http://192.168.20.10:8766"

PORT = 8777

WIDTH = 1280
HEIGHT = 720

ROI_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_left_roi_v2.json"
)

THRESH_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_left_thresholds_v1.json"
)

GOLDEN_RGB = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_left_golden_v1/golden_rgb_median.png"
)

GOLDEN_DEPTH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_left_golden_v1/golden_depth_median.png"
)

LIFT_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_left_lift_thresholds_v1.json"
)

FETCH_TIMEOUT = 3.0
FETCH_RETRY = 3
LOOP_DELAY = 0.25


lock = threading.Lock()

latest_jpeg = None

latest_status = {
    "inspection": "PRE_ROOF",
    "view": "LEFT",
    "result": "NOT_EVALUATED",
    "reason": "STARTING",
    "production_valid": False,
    "rois": {},
}

running = True


def fetch(path):

    last = None

    for _ in range(FETCH_RETRY):

        try:

            with urllib.request.urlopen(
                SOURCE + path,
                timeout=FETCH_TIMEOUT
            ) as r:
                data = r.read()

            img = cv2.imdecode(
                np.frombuffer(
                    data,
                    np.uint8
                ),
                cv2.IMREAD_COLOR
            )

            if img is None:
                raise RuntimeError(
                    f"decode failed: {path}"
                )

            if img.shape[:2] != (720, 1280):
                raise RuntimeError(
                    f"bad shape {path}: {img.shape}"
                )

            # VIEW_LEFT canonical orientation
            return cv2.rotate(
                img,
                cv2.ROTATE_180
            )

        except Exception as e:

            last = e
            time.sleep(0.15)

    raise RuntimeError(
        f"fetch failed {path}: {last}"
    )


def metrics(current, golden):

    a = current.astype(np.int16)
    b = golden.astype(np.int16)

    diff = np.abs(a - b)

    gray_a = cv2.cvtColor(
        current,
        cv2.COLOR_BGR2GRAY
    )

    gray_b = cv2.cvtColor(
        golden,
        cv2.COLOR_BGR2GRAY
    )

    edge_a = cv2.Canny(
        gray_a,
        50,
        120
    )

    edge_b = cv2.Canny(
        gray_b,
        50,
        120
    )

    edge_xor = cv2.bitwise_xor(
        edge_a,
        edge_b
    )

    return {
        "mean_absdiff":
            float(np.mean(diff)),

        "p95_absdiff":
            float(np.percentile(diff, 95)),

        "changed_fraction_20":
            float(
                np.mean(
                    np.max(diff, axis=2) > 20
                )
            ),

        "edge_change_fraction":
            float(
                np.mean(edge_xor > 0)
            ),
    }


def evaluate_source(
    values,
    thresholds
):

    detail = {}

    for metric, value in values.items():

        threshold = thresholds[
            metric
        ]["threshold"]

        detail[metric] = {
            "value":
                value,

            "threshold":
                threshold,

            "exceeded":
                value > threshold,
        }

    count = sum(
        1
        for item in detail.values()
        if item["exceeded"]
    )

    return (
        count >= 2,
        count,
        detail,
    )


roi_cfg = json.loads(
    ROI_CONFIG.read_text(
        encoding="utf-8"
    )
)

threshold_cfg = json.loads(
    THRESH_CONFIG.read_text(
        encoding="utf-8"
    )
)

lift_cfg = json.loads(
    LIFT_CONFIG.read_text(
        encoding="utf-8"
    )
)

rois = roi_cfg["rois"]


gold_rgb = cv2.imread(
    str(GOLDEN_RGB)
)

gold_depth = cv2.imread(
    str(GOLDEN_DEPTH)
)

if gold_rgb is None:
    raise SystemExit(
        f"Missing Golden RGB: {GOLDEN_RGB}"
    )

if gold_depth is None:
    raise SystemExit(
        f"Missing Golden Depth: {GOLDEN_DEPTH}"
    )


def gray_template_shift(
    current,
    golden,
    roi
):

    margin = int(
        lift_cfg["decision"][
            "search_margin_px"
        ]
    )

    x = int(roi["x"])
    y = int(roi["y"])
    w = int(roi["w"])
    h = int(roi["h"])

    H, W = current.shape[:2]

    sx0 = max(
        0,
        x - margin
    )

    sy0 = max(
        0,
        y - margin
    )

    sx1 = min(
        W,
        x + w + margin
    )

    sy1 = min(
        H,
        y + h + margin
    )

    cur_search = current[
        sy0:sy1,
        sx0:sx1
    ]

    ref_patch = golden[
        y:y+h,
        x:x+w
    ]

    clahe = cv2.createCLAHE(
        clipLimit=2.0,
        tileGridSize=(8, 8)
    )

    cur_gray = clahe.apply(
        cv2.cvtColor(
            cur_search,
            cv2.COLOR_BGR2GRAY
        )
    )

    ref_gray = clahe.apply(
        cv2.cvtColor(
            ref_patch,
            cv2.COLOR_BGR2GRAY
        )
    )

    result = cv2.matchTemplate(
        cur_gray,
        ref_gray,
        cv2.TM_CCOEFF_NORMED
    )

    _, score, _, loc = (
        cv2.minMaxLoc(result)
    )

    match_x = (
        sx0 + loc[0]
    )

    match_y = (
        sy0 + loc[1]
    )

    dx = int(
        match_x - x
    )

    dy = int(
        match_y - y
    )

    return {
        "dx": dx,
        "dy": dy,
        "score": float(score),
    }


def evaluate(
    rgb,
    depth
):

    results = {}

    for item in rois:

        name = item["name"]

        x = int(item["x"])
        y = int(item["y"])
        w = int(item["w"])
        h = int(item["h"])

        rgb_now = rgb[
            y:y+h,
            x:x+w
        ]

        rgb_ref = gold_rgb[
            y:y+h,
            x:x+w
        ]

        dep_now = depth[
            y:y+h,
            x:x+w
        ]

        dep_ref = gold_depth[
            y:y+h,
            x:x+w
        ]

        rgb_values = metrics(
            rgb_now,
            rgb_ref
        )

        dep_values = metrics(
            dep_now,
            dep_ref
        )

        cfg = threshold_cfg[
            "rois"
        ][name]

        rgb_fail, rgb_count, rgb_detail = (
            evaluate_source(
                rgb_values,
                cfg["rgb"]
            )
        )

        dep_fail, dep_count, dep_detail = (
            evaluate_source(
                dep_values,
                cfg["depth"]
            )
        )

        # WALL_BODY:
        #   Keep original conservative rule.
        #
        # CORNER:
        #   Also accept cross-source corroboration.
        #   One RGB metric + one DEPTH metric
        #   is considered a valid corner abnormality.
        cross_source_fail = (
            item["group"] == "CORNER"
            and rgb_count >= 1
            and dep_count >= 1
        )

        roi_fail = (
            rgb_fail
            or dep_fail
            or cross_source_fail
        )

        lift_check = None
        lift_fail = False

        if name == lift_cfg["target_roi"]:

            lift_check = gray_template_shift(
                rgb,
                gold_rgb,
                item
            )

            dy_limit = int(
                lift_cfg["decision"][
                    "gray_dy_fail_if_le"
                ]
            )

            min_score = float(
                lift_cfg["decision"][
                    "minimum_gray_score"
                ]
            )

            lift_fail = (
                lift_check["score"] >= min_score
                and lift_check["dy"] <= dy_limit
            )

            if lift_fail:
                roi_fail = True

        results[name] = {
            "group":
                item["group"],

            "result":
                "FAIL"
                if roi_fail
                else "PASS",

            "rgb_fail":
                rgb_fail,

            "depth_fail":
                dep_fail,

            "rgb_metric_fail_count":
                rgb_count,

            "depth_metric_fail_count":
                dep_count,

            "cross_source_fail":
                cross_source_fail,

            "lift_fail":
                lift_fail,

            "lift_check":
                lift_check,

            "rgb":
                rgb_detail,

            "depth":
                dep_detail,
        }

    view_result = (
        "PASS"
        if all(
            item["result"] == "PASS"
            for item in results.values()
        )
        else "FAIL"
    )

    return (
        view_result,
        results
    )


def draw_overlay(
    image,
    results
):

    out = image.copy()

    for item in rois:

        name = item["name"]

        x = int(item["x"])
        y = int(item["y"])
        w = int(item["w"])
        h = int(item["h"])

        result = results[
            name
        ]["result"]

        color = (
            (0, 210, 0)
            if result == "PASS"
            else (0, 0, 255)
        )

        cv2.rectangle(
            out,
            (x, y),
            (x+w, y+h),
            color,
            2
        )

        cv2.putText(
            out,
            name,
            (
                x,
                max(20, y-5)
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA
        )

    return out


def fit(
    img,
    width,
    height
):

    ih, iw = img.shape[:2]

    scale = min(
        width / iw,
        height / ih
    )

    nw = int(
        iw * scale
    )

    nh = int(
        ih * scale
    )

    resized = cv2.resize(
        img,
        (nw, nh),
        interpolation=cv2.INTER_AREA
    )

    canvas = np.zeros(
        (
            height,
            width,
            3
        ),
        dtype=np.uint8
    )

    x = (
        width - nw
    ) // 2

    y = (
        height - nh
    ) // 2

    canvas[
        y:y+nh,
        x:x+nw
    ] = resized

    return canvas


def build_dashboard(
    rgb,
    depth,
    view_result,
    results
):

    canvas = np.zeros(
        (
            HEIGHT,
            WIDTH,
            3
        ),
        dtype=np.uint8
    )

    # Header
    cv2.rectangle(
        canvas,
        (0, 0),
        (WIDTH, 62),
        (24, 24, 24),
        -1
    )

    cv2.putText(
        canvas,
        "HARMONY PRE-ROOF QUALITY INSPECTION",
        (25, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.82,
        (240, 240, 240),
        2,
        cv2.LINE_AA
    )

    result_color = (
        (0, 220, 0)
        if view_result == "PASS"
        else (0, 0, 255)
    )

    cv2.putText(
        canvas,
        f"VIEW_LEFT : {view_result}",
        (1000, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.70,
        result_color,
        2,
        cv2.LINE_AA
    )

    # RGB overlay
    overlay = draw_overlay(
        rgb,
        results
    )

    rgb_panel = fit(
        overlay,
        760,
        428
    )

    canvas[
        78:506,
        18:778
    ] = rgb_panel

    cv2.putText(
        canvas,
        "RGB QC + ROI / ROT 180",
        (20, 72),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (210, 210, 210),
        1,
        cv2.LINE_AA
    )

    # Depth
    depth_panel = fit(
        depth,
        470,
        264
    )

    canvas[
        78:342,
        800:1270
    ] = depth_panel

    cv2.putText(
        canvas,
        "DEPTH SHAPE / ROT 180",
        (802, 72),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (210, 210, 210),
        1,
        cv2.LINE_AA
    )

    # Bottom-left info
    cv2.putText(
        canvas,
        "ROBOT POSE",
        (35, 555),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (180, 180, 180),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_LEFT / FIXED",
        (200, 555),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (240, 240, 240),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "QC INPUT",
        (35, 595),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (180, 180, 180),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "D435 RGB + DEPTH SHAPE",
        (200, 595),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (240, 240, 240),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "LEFT RESULT",
        (35, 635),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (180, 180, 180),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        view_result,
        (200, 635),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        result_color,
        2,
        cv2.LINE_AA
    )

    # ROI result list
    y = 385

    for item in rois:

        name = item["name"]

        result = results[
            name
        ]["result"]

        color = (
            (0, 220, 0)
            if result == "PASS"
            else (0, 0, 255)
        )

        cv2.putText(
            canvas,
            name,
            (820, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (210, 210, 210),
            1,
            cv2.LINE_AA
        )

        cv2.putText(
            canvas,
            result,
            (1135, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            color,
            1,
            cv2.LINE_AA
        )

        y += 32

    return canvas


def build_not_evaluated(
    reason
):

    canvas = np.zeros(
        (
            HEIGHT,
            WIDTH,
            3
        ),
        dtype=np.uint8
    )

    cv2.putText(
        canvas,
        "HARMONY PRE-ROOF VIEW_LEFT",
        (45, 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (240, 240, 240),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "NOT_EVALUATED",
        (45, 145),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 180, 255),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        reason[:100],
        (45, 210),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (220, 220, 220),
        1,
        cv2.LINE_AA
    )

    return canvas


def worker():

    global latest_jpeg
    global latest_status

    while running:

        try:

            rgb = fetch(
                "/raw"
            )

            depth = fetch(
                "/depthimg"
            )

            view_result, results = evaluate(
                rgb,
                depth
            )

            dashboard = build_dashboard(
                rgb,
                depth,
                view_result,
                results
            )

            status = {
                "inspection":
                    "PRE_ROOF",

                "view":
                    "LEFT",

                "result":
                    view_result,

                "timestamp":
                    time.time(),

                "production_valid":
                    False,

                "canonical_transform": {
                    "rotation_deg":
                        180
                },

                "rois":
                    results,
            }

        except Exception as e:

            reason = str(e)

            dashboard = (
                build_not_evaluated(
                    reason
                )
            )

            status = {
                "inspection":
                    "PRE_ROOF",

                "view":
                    "LEFT",

                "result":
                    "NOT_EVALUATED",

                "reason":
                    reason,

                "timestamp":
                    time.time(),

                "production_valid":
                    False,

                "rois":
                    {},
            }

        ok, jpg = cv2.imencode(
            ".jpg",
            dashboard,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                90
            ]
        )

        if ok:

            with lock:

                latest_jpeg = (
                    jpg.tobytes()
                )

                latest_status = (
                    status
                )

        time.sleep(
            LOOP_DELAY
        )


class Handler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        *args
    ):
        pass


    def do_GET(self):

        if self.path.startswith(
            "/status"
        ):

            with lock:
                payload = json.dumps(
                    latest_status,
                    indent=2
                ).encode(
                    "utf-8"
                )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                str(len(payload))
            )

            self.end_headers()

            self.wfile.write(
                payload
            )

            return


        if self.path in (
            "/",
            "/stream"
        ):

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame"
            )

            self.end_headers()

            while True:

                with lock:
                    jpg = latest_jpeg

                if jpg is None:
                    time.sleep(0.05)
                    continue

                try:

                    self.wfile.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + jpg +
                        b"\r\n"
                    )

                except Exception:
                    break

                time.sleep(0.08)

            return


        if self.path == "/frame.jpg":

            with lock:
                jpg = latest_jpeg

            if jpg is None:
                self.send_error(
                    503
                )
                return

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "image/jpeg"
            )

            self.send_header(
                "Content-Length",
                str(len(jpg))
            )

            self.end_headers()

            self.wfile.write(
                jpg
            )

            return


        self.send_error(
            404
        )


threading.Thread(
    target=worker,
    daemon=True
).start()


print()
print("============================================")
print(" HARMONY PRE-ROOF VIEW_LEFT QC RUNTIME V3")
print("============================================")
print("VIEW       : LEFT")
print(f"QC URL     : http://127.0.0.1:{PORT}/")
print(f"STATUS     : http://127.0.0.1:{PORT}/status")
print(f"INPUT      : {SOURCE}")
print("TRANSFORM  : ROTATE 180")
print(f"ROI COUNT  : {len(rois)}")
print("ROBOT CTRL : NONE")
print("SERVER TX  : NOT YET ENABLED")
print("UNITY TX   : QC MJPEG READY")
print("VALIDITY   : DEVELOPMENT / production_valid=false")
print("============================================")


ThreadingHTTPServer(
    (
        "0.0.0.0",
        PORT
    ),
    Handler
).serve_forever()
