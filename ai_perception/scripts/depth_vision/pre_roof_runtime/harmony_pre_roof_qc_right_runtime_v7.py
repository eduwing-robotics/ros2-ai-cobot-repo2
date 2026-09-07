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
PORT = 8785

GOLDEN_RGB_PATH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_right_golden_v2/golden_rgb_median.png"
)

GOLDEN_DEPTH_PATH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_right_golden_v2/golden_depth_median.png"
)

ROI_PATH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_right_roi_v1.json"
)

THRESH_PATH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_right_thresholds_v3.json"
)

LIFT_THRESH_PATH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_right_lift_thresholds_v2.json"
)

FETCH_TIMEOUT = 3.0
LOOP_SEC = 0.15


gold_rgb = cv2.imread(str(GOLDEN_RGB_PATH))
gold_depth = cv2.imread(str(GOLDEN_DEPTH_PATH))

if gold_rgb is None:
    raise SystemExit(
        f"ABORT: missing Golden RGB: {GOLDEN_RGB_PATH}"
    )

if gold_depth is None:
    raise SystemExit(
        f"ABORT: missing Golden Depth: {GOLDEN_DEPTH_PATH}"
    )

roi_cfg = json.loads(
    ROI_PATH.read_text(encoding="utf-8")
)

thr_cfg = json.loads(
    THRESH_PATH.read_text(encoding="utf-8")
)

rois = roi_cfg["rois"]
thresholds = thr_cfg["rois"]

lift_cfg = json.loads(
    LIFT_THRESH_PATH.read_text(
        encoding="utf-8"
    )
)

lift_thresholds = lift_cfg["corners"]

roi_by_name = {
    item["name"]: item
    for item in rois
}


lock = threading.Lock()

latest_status = {
    "schema": "harmony_pre_roof_view_right_runtime_v7",
    "inspection": "PRE_ROOF",
    "view": "RIGHT",
    "result": "NOT_EVALUATED",
    "runtime": "V7",
    "runtime_port": PORT,
    "robot_pose": "VIEW_RIGHT_FIXED",
    "transform": "ROTATE_180",
    "source": SOURCE,
    "rois": {},
    "production_valid": False,
}

latest_jpeg = None
running = True


def fetch(path):

    with urllib.request.urlopen(
        SOURCE + path,
        timeout=FETCH_TIMEOUT
    ) as r:
        data = r.read()

    arr = np.frombuffer(
        data,
        np.uint8
    )

    img = cv2.imdecode(
        arr,
        cv2.IMREAD_COLOR
    )

    if img is None:
        raise RuntimeError(
            f"decode failed: {path}"
        )

    if img.shape[:2] != (720, 1280):
        raise RuntimeError(
            f"unexpected shape {path}: {img.shape}"
        )

    return cv2.rotate(
        img,
        cv2.ROTATE_180
    )


def metrics(current, golden):

    diff = np.abs(
        current.astype(np.int16) -
        golden.astype(np.int16)
    )

    ga = cv2.cvtColor(
        current,
        cv2.COLOR_BGR2GRAY
    )

    gb = cv2.cvtColor(
        golden,
        cv2.COLOR_BGR2GRAY
    )

    ea = cv2.Canny(
        ga,
        50,
        120
    )

    eb = cv2.Canny(
        gb,
        50,
        120
    )

    edge_xor = cv2.bitwise_xor(
        ea,
        eb
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


def prep_gray_shift(img):

    g = cv2.cvtColor(
        img,
        cv2.COLOR_BGR2GRAY
    )

    return cv2.GaussianBlur(
        g,
        (5, 5),
        0
    )


def gray_template_shift(
    current,
    golden,
    roi,
    margin=24
):

    cur = prep_gray_shift(current)
    ref = prep_gray_shift(golden)

    H, W = cur.shape[:2]

    x = int(roi["x"])
    y = int(roi["y"])
    w = int(roi["w"])
    h = int(roi["h"])

    template = ref[
        y:y+h,
        x:x+w
    ]

    sx1 = max(
        0,
        x - margin
    )

    sy1 = max(
        0,
        y - margin
    )

    sx2 = min(
        W,
        x + w + margin
    )

    sy2 = min(
        H,
        y + h + margin
    )

    search = cur[
        sy1:sy2,
        sx1:sx2
    ]

    result = cv2.matchTemplate(
        search,
        template,
        cv2.TM_CCOEFF_NORMED
    )

    _, score, _, loc = cv2.minMaxLoc(
        result
    )

    matched_x = sx1 + loc[0]
    matched_y = sy1 + loc[1]

    return {
        "dx":
            int(matched_x - x),

        "dy":
            int(matched_y - y),

        "score":
            float(score)
    }


def evaluate_source(
    name,
    source_name,
    values
):

    cfg = thresholds[name][source_name]

    details = {}

    count = 0

    for metric, value in values.items():

        threshold = float(
            cfg[metric]["threshold"]
        )

        exceeded = (
            value > threshold
        )

        if exceeded:
            count += 1

        details[metric] = {
            "value": value,
            "threshold": threshold,
            "exceeded": exceeded,
        }

    return (
        count >= 2,
        count,
        details
    )


def evaluate(rgb, depth):

    output = {}

    for item in rois:

        name = item["name"]

        x = item["x"]
        y = item["y"]
        w = item["w"]
        h = item["h"]

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

        (
            rgb_fail,
            rgb_count,
            rgb_details
        ) = evaluate_source(
            name,
            "rgb",
            rgb_values
        )

        (
            depth_fail,
            depth_count,
            depth_details
        ) = evaluate_source(
            name,
            "depth",
            dep_values
        )

        roi_fail = (
            rgb_fail
            or depth_fail
        )

        output[name] = {
            "result":
                "FAIL"
                if roi_fail
                else "PASS",

            "rgb_fail":
                rgb_fail,

            "depth_fail":
                depth_fail,

            "rgb_metric_fail_count":
                rgb_count,

            "depth_metric_fail_count":
                depth_count,

            "cross_source_fail":
                False,

            "lift_fail":
                False,

            "lift_check":
                None,

            "rgb":
                rgb_details,

            "depth":
                depth_details,
        }

    # ==================================================
    # VIEW_RIGHT HIERARCHICAL QC POLICY V4
    # ==================================================
    #
    # STAGE 1
    # WALL_BODY FAIL
    #   -> VIEW FAIL
    #   -> both corners forced FAIL
    #
    # STAGE 2
    # WALL_BODY PASS
    #   -> GRAY template shift seating/lift inspection
    #   -> CORNER_LEFT / CORNER_RIGHT independent
    #
    # Existing corner RGB/DEPTH image-diff values remain
    # diagnostic only.

    wall_fail = (
        output["WALL_BODY"]["result"]
        == "FAIL"
    )

    output[
        "WALL_BODY"
    ]["decision_role"] = "PRIMARY_WALL_QC"

    output[
        "WALL_BODY"
    ]["effective_result"] = (
        output["WALL_BODY"]["result"]
    )

    output[
        "WALL_BODY"
    ]["reason"] = (
        "WALL_BODY_FAIL"
        if wall_fail
        else "WALL_BODY_PASS"
    )

    for name in (
        "CORNER_LEFT",
        "CORNER_RIGHT",
    ):

        raw_result = (
            output[name]["result"]
        )

        output[
            name
        ]["raw_image_diff_result"] = (
            raw_result
        )

        output[
            name
        ]["decision_role"] = (
            "SECONDARY_LIFT_QC"
        )

        if wall_fail:

            output[
                name
            ]["effective_result"] = "FAIL"

            output[
                name
            ]["result"] = "FAIL"

            output[
                name
            ]["lift_fail"] = False

            output[
                name
            ]["lift_check"] = None

            output[
                name
            ]["reason"] = (
                "WALL_BODY_FAIL_OVERRIDE"
            )

            continue

        check = gray_template_shift(
            rgb,
            gold_rgb,
            roi_by_name[name],
            int(
                lift_cfg[
                    "search_margin_px"
                ]
            )
        )

        cfg = lift_thresholds[name]

        dy_threshold = int(
            cfg[
                "gray_dy_fail_if_lte"
            ]
        )

        min_score = float(
            cfg[
                "min_gray_score"
            ]
        )

        low_confidence = (
            check["score"]
            < min_score
        )

        # LEFT:
        #   normal and lift have large DY separation,
        #   so the configured DY threshold is sufficient.
        #
        # RIGHT:
        #   final normal recovery reached DY=-11,
        #   while the weakest validated lift reached DY=-12.
        #   Use score only for this one-pixel boundary case.
        #
        #   DY <= -13:
        #       definite lift
        #
        #   DY == -12 AND score <= 0.963:
        #       validated borderline lift signature
        #
        #   otherwise:
        #       PASS

        if name == "CORNER_RIGHT":

            severe_dy_lift = (
                check["dy"] <= -13
            )

            borderline_lift = (
                check["dy"] == -12
                and check["score"] <= 0.963
            )

            dy_lift = (
                severe_dy_lift
                or borderline_lift
            )

        else:

            severe_dy_lift = (
                check["dy"]
                <= dy_threshold
            )

            borderline_lift = False

            dy_lift = (
                severe_dy_lift
            )

        lift_fail = (
            low_confidence
            or dy_lift
        )

        output[name][
            "lift_check"
        ] = {
            "method":
                "GRAY_TEMPLATE_SHIFT",

            "dx":
                check["dx"],

            "dy":
                check["dy"],

            "score":
                check["score"],

            "dy_fail_if_lte":
                dy_threshold,

            "min_gray_score":
                min_score,

            "dx_role":
                "DIAGNOSTIC_ONLY",

            "low_confidence":
                low_confidence,

            "dy_lift":
                dy_lift,

            "severe_dy_lift":
                severe_dy_lift,

            "borderline_lift":
                borderline_lift,

            "right_borderline_score_max":
                0.963
                if name == "CORNER_RIGHT"
                else None
        }

        output[
            name
        ]["lift_fail"] = (
            lift_fail
        )

        output[
            name
        ]["result"] = (
            "FAIL"
            if lift_fail
            else "PASS"
        )

        output[
            name
        ]["effective_result"] = (
            output[name]["result"]
        )

        if low_confidence:

            output[
                name
            ]["reason"] = (
                "GRAY_MATCH_LOW_CONFIDENCE"
            )

        elif dy_lift:

            output[
                name
            ]["reason"] = (
                "LIFT_FAIL"
            )

        else:

            output[
                name
            ]["reason"] = (
                "LIFT_PASS"
            )

    if wall_fail:

        view_result = "FAIL"

    else:

        corner_fail = any(
            output[name]["result"]
            == "FAIL"
            for name in (
                "CORNER_LEFT",
                "CORNER_RIGHT",
            )
        )

        view_result = (
            "FAIL"
            if corner_fail
            else "PASS"
        )

    return (
        view_result,
        output
    )


def make_dashboard(
    rgb,
    depth,
    result,
    roi_results
):

    annotated = rgb.copy()

    for item in rois:

        name = item["name"]

        x = item["x"]
        y = item["y"]
        w = item["w"]
        h = item["h"]

        fail = (
            roi_results[name]["result"]
            == "FAIL"
        )

        color = (
            (0, 0, 255)
            if fail
            else (0, 255, 0)
        )

        cv2.rectangle(
            annotated,
            (x, y),
            (x+w, y+h),
            color,
            3
        )

        cv2.putText(
            annotated,
            name,
            (x, max(24, y-7)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA
        )

    canvas = np.zeros(
        (720, 1280, 3),
        dtype=np.uint8
    )

    cv2.putText(
        canvas,
        "HARMONY PRE-ROOF QUALITY INSPECTION",
        (35, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.92,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )

    result_color = (
        (0, 255, 0)
        if result == "PASS"
        else (0, 0, 255)
    )

    cv2.putText(
        canvas,
        f"VIEW_RIGHT : {result}",
        (930, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.82,
        result_color,
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "RGB QC + ROI / ROT 180",
        (35, 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (200, 200, 200),
        1,
        cv2.LINE_AA
    )

    rgb_panel = cv2.resize(
        annotated,
        (700, 394)
    )

    canvas[
        90:484,
        35:735
    ] = rgb_panel

    cv2.putText(
        canvas,
        "DEPTH SHAPE / ROT 180",
        (770, 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (200, 200, 200),
        1,
        cv2.LINE_AA
    )

    depth_panel = cv2.resize(
        depth,
        (470, 264)
    )

    canvas[
        90:354,
        770:1240
    ] = depth_panel

    yy = 395

    for item in rois:

        name = item["name"]

        roi_result = (
            roi_results[name]["result"]
        )

        color = (
            (0, 255, 0)
            if roi_result == "PASS"
            else (0, 0, 255)
        )

        cv2.putText(
            canvas,
            name,
            (790, yy),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (210, 210, 210),
            1,
            cv2.LINE_AA
        )

        cv2.putText(
            canvas,
            roi_result,
            (1120, yy),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA
        )

        yy += 32

    cv2.putText(
        canvas,
        "ROBOT POSE",
        (55, 545),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_RIGHT / FIXED",
        (220, 545),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.60,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "QC INPUT",
        (55, 590),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "D435 RGB + DEPTH SHAPE",
        (220, 590),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.60,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "RIGHT RESULT",
        (55, 635),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        result,
        (220, 635),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        result_color,
        2,
        cv2.LINE_AA
    )

    return canvas


def worker():

    global latest_status
    global latest_jpeg

    while running:

        try:
            rgb = fetch("/raw")
            depth = fetch("/depthimg")

            result, roi_results = evaluate(
                rgb,
                depth
            )

            dashboard = make_dashboard(
                rgb,
                depth,
                result,
                roi_results
            )

            ok, enc = cv2.imencode(
                ".jpg",
                dashboard,
                [
                    int(
                        cv2.IMWRITE_JPEG_QUALITY
                    ),
                    92
                ]
            )

            if not ok:
                raise RuntimeError(
                    "dashboard JPEG encode failed"
                )

            status = {
                "schema":
                    "harmony_pre_roof_view_right_runtime_v7",

                "inspection":
                    "PRE_ROOF",

                "view":
                    "RIGHT",

                "result":
                    result,

                "runtime":
                    "V7",

                "runtime_port":
                    PORT,

                "robot_pose":
                    "VIEW_RIGHT_FIXED",

                "transform":
                    "ROTATE_180",

                "source":
                    SOURCE,

                "golden":
                    "view_right_golden_v2",

                "roi_config":
                    "view_right_roi_v1.json",

                "thresholds":
                    "view_right_thresholds_v3.json",

                "decision_policy": {
                    "stage_1":
                        "WALL_BODY",

                    "stage_1_fail":
                        "FORCE_VIEW_AND_BOTH_CORNERS_FAIL",

                    "stage_2":
                        "CORNER_LEFT_AND_CORNER_RIGHT_LIFT",

                    "corner_rgb_depth":
                        "DIAGNOSTIC_ONLY",

                    "lift_thresholds":
                        "view_right_lift_thresholds_v2.json",

                    "corner_lift_status":
                        "ACTIVE_V2"
                },

                "rois":
                    roi_results,

                "timestamp":
                    time.time(),

                "production_valid":
                    False
            }

            with lock:
                latest_status = status
                latest_jpeg = enc.tobytes()

        except Exception as e:

            status = {
                "schema":
                    "harmony_pre_roof_view_right_runtime_v7",

                "inspection":
                    "PRE_ROOF",

                "view":
                    "RIGHT",

                "result":
                    "NOT_EVALUATED",

                "runtime":
                    "V7",

                "runtime_port":
                    PORT,

                "error":
                    str(e),

                "timestamp":
                    time.time(),

                "production_valid":
                    False
            }

            with lock:
                latest_status = status

            print(
                "[RUNTIME WARNING]",
                e
            )

        time.sleep(
            LOOP_SEC
        )


class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        fmt,
        *args
    ):
        return

    def do_GET(self):

        if self.path == "/status":

            with lock:
                body = json.dumps(
                    latest_status,
                    ensure_ascii=False
                ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()
            self.wfile.write(body)

            return

        if self.path == "/raw":

            with lock:
                frame = latest_jpeg

            if frame is None:
                self.send_error(
                    503,
                    "dashboard not ready"
                )
                return

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "image/jpeg"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.send_header(
                "Content-Length",
                str(len(frame))
            )

            self.end_headers()
            self.wfile.write(frame)

            return

        if self.path == "/stream":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.end_headers()

            while running:

                with lock:
                    frame = latest_jpeg

                if frame is None:
                    time.sleep(0.05)
                    continue

                try:
                    self.wfile.write(
                        b"--frame\r\n"
                    )

                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\n"
                    )

                    self.wfile.write(
                        f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    )

                    self.wfile.write(
                        frame
                    )

                    self.wfile.write(
                        b"\r\n"
                    )

                    self.wfile.flush()

                except Exception:
                    break

                time.sleep(0.10)

            return

        if self.path == "/":

            html = b"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Harmony PRE-ROOF RIGHT QC</title>
<style>
html,body {
    margin:0;
    background:#151515;
    width:100%;
    height:100%;
    overflow:hidden;
}
body {
    display:flex;
    justify-content:center;
    align-items:center;
}
img {
    width:min(100vw,1280px);
    height:auto;
}
</style>
</head>
<body>
<img src="/stream">
</body>
</html>
"""

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.send_header(
                "Content-Length",
                str(len(html))
            )

            self.end_headers()
            self.wfile.write(html)

            return

        self.send_error(404)


threading.Thread(
    target=worker,
    daemon=True
).start()


print()
print("============================================")
print(" HARMONY PRE-ROOF VIEW_RIGHT QC RUNTIME V7")
print("============================================")
print("VIEW       : RIGHT")
print(f"QC URL     : http://127.0.0.1:{PORT}/")
print(f"STATUS     : http://127.0.0.1:{PORT}/status")
print("INPUT      :", SOURCE)
print("TRANSFORM  : ROTATE 180")
print("ROI COUNT  :", len(rois))
print("GOLDEN     : V2")
print("THRESHOLD  : V3")
print("ROBOT CTRL : NONE")
print("SERVER TX  : NOT YET ENABLED")
print("UNITY TX   : QC MJPEG READY")
print("VALIDITY   : DEVELOPMENT / production_valid=false")
print("============================================")

server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    Handler
)

try:
    server.serve_forever()

except KeyboardInterrupt:
    pass

finally:
    running = False
    server.server_close()
