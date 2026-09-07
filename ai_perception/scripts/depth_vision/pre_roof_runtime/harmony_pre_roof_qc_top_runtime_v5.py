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

PORT = 8773
WIDTH = 1280
HEIGHT = 720

ROI_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_roi_v3.json"
)

THRESH_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_thresholds_v2.json"
)

GOLDEN_RGB = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_golden_v2/golden_rgb_median.png"
)

GOLDEN_DEPTH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_golden_v2/golden_depth_median.png"
)


BASE_HOLE_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_base_holes_v1/base_holes_config.json"
)

BASE_HOLE_THRESH_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_base_holes_thresholds_v1.json"
)

FETCH_TIMEOUT = 3.0
FETCH_RETRY = 3
LOOP_DELAY = 0.25

lock = threading.Lock()

latest_jpeg = None
latest_status = {
    "view": "TOP",
    "result": "NOT_EVALUATED",
    "reason": "STARTING",
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

            arr = np.frombuffer(
                data,
                dtype=np.uint8
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
                    f"bad shape {path}: {img.shape}"
                )

            return img

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
    exceeded = {}

    for metric, value in values.items():
        threshold = thresholds[
            metric
        ]["threshold"]

        exceeded[metric] = {
            "value": value,
            "threshold": threshold,
            "exceeded": value > threshold,
        }

    count = sum(
        1
        for x in exceeded.values()
        if x["exceeded"]
    )

    return (
        count >= 2,
        count,
        exceeded,
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


base_hole_cfg = json.loads(
    BASE_HOLE_CONFIG.read_text(
        encoding="utf-8"
    )
)

base_hole_threshold_cfg = json.loads(
    BASE_HOLE_THRESH_CONFIG.read_text(
        encoding="utf-8"
    )
)

base_holes = base_hole_cfg["holes"]


def hole_metrics(current, golden):
    """
    Dedicated individual-hole metrics.

    Important:
    This matches the validated Hole Probe V1.
    It intentionally does NOT reuse the large-ROI
    changed_fraction implementation.
    """

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

    gray_diff = cv2.absdiff(
        gray_a,
        gray_b
    )

    return {
        "mean_absdiff":
            float(np.mean(diff)),

        "p95_absdiff":
            float(np.percentile(diff, 95)),

        "changed_fraction_20":
            float(
                np.mean(
                    gray_diff > 20
                )
            ),
    }


def evaluate_hole_source(
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
        detail
    )



def evaluate(rgb, depth):

    results = {}

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

        roi_fail = (
            rgb_fail or dep_fail
        )

        results[name] = {
            "group": item["group"],
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
            "rgb":
                rgb_detail,
            "depth":
                dep_detail,
        }


    # -------------------------------------------------
    # Individual BASE HOLE inspection
    # -------------------------------------------------

    hole_results = {}

    for hole in base_holes:

        hole_name = hole["name"]

        x = int(hole["x"])
        y = int(hole["y"])
        w = int(hole["w"])
        h = int(hole["h"])

        rgb_now = rgb[
            y:y+h,
            x:x+w
        ]

        rgb_ref = gold_rgb[
            y:y+h,
            x:x+w
        ]

        values = hole_metrics(
            rgb_now,
            rgb_ref
        )

        thresholds = (
            base_hole_threshold_cfg[
                "holes"
            ][hole_name]
        )

        hole_fail, fail_count, detail = (
            evaluate_hole_source(
                values,
                thresholds
            )
        )

        hole_results[hole_name] = {
            "result":
                "FAIL"
                if hole_fail
                else "PASS",

            "rgb_metric_fail_count":
                fail_count,

            "rgb":
                detail,

            "roi": {
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            }
        }

    hole_fail_count = sum(
        1
        for item in hole_results.values()
        if item["result"] == "FAIL"
    )

    base_large_result = (
        results["BASE_HOLES"]["result"]
    )

    results["BASE_HOLES"][
        "large_roi_result"
    ] = base_large_result

    results["BASE_HOLES"][
        "holes"
    ] = hole_results

    results["BASE_HOLES"][
        "hole_fail_count"
    ] = hole_fail_count

    # Parent BASE_HOLES is FAIL if:
    # - original large ROI fails, OR
    # - any individual hole fails.
    if (
        base_large_result == "FAIL"
        or hole_fail_count > 0
    ):
        results["BASE_HOLES"][
            "result"
        ] = "FAIL"

    view_result = (
        "PASS"
        if all(
            x["result"] == "PASS"
            for x in results.values()
        )
        else "FAIL"
    )

    return view_result, results


def draw_roi_overlay(
    image,
    results
):
    out = image.copy()

    for item in rois:

        name = item["name"]

        x = item["x"]
        y = item["y"]
        w = item["w"]
        h = item["h"]

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
                max(18, y-5)
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            color,
            1,
            cv2.LINE_AA
        )


    # Individual hole overlay
    base_result = results.get(
        "BASE_HOLES",
        {}
    )

    hole_results = base_result.get(
        "holes",
        {}
    )

    for hole in base_holes:

        name = hole["name"]

        if name not in hole_results:
            continue

        x = int(hole["x"])
        y = int(hole["y"])
        w = int(hole["w"])
        h = int(hole["h"])

        result = hole_results[
            name
        ]["result"]

        color = (
            (0, 220, 220)
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
            name.replace("HOLE_", "H"),
            (
                x,
                max(18, y-4)
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.36,
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

    nw = int(iw * scale)
    nh = int(ih * scale)

    resized = cv2.resize(
        img,
        (nw, nh),
        interpolation=cv2.INTER_AREA
    )

    canvas = np.zeros(
        (height, width, 3),
        dtype=np.uint8
    )

    x = (width - nw) // 2
    y = (height - nh) // 2

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
        (HEIGHT, WIDTH, 3),
        dtype=np.uint8
    )

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
        f"VIEW_TOP : {view_result}",
        (990, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        result_color,
        2,
        cv2.LINE_AA
    )

    rgb_overlay = draw_roi_overlay(
        rgb,
        results
    )

    rgb_panel = fit(
        rgb_overlay,
        760,
        428
    )

    depth_panel = fit(
        depth,
        470,
        264
    )

    canvas[
        82:510,
        20:780
    ] = rgb_panel

    canvas[
        82:346,
        795:1265
    ] = depth_panel

    cv2.putText(
        canvas,
        "RGB QC + ROI",
        (20, 76),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (210, 210, 210),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "DEPTH SHAPE VIEW",
        (795, 76),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (210, 210, 210),
        1,
        cv2.LINE_AA
    )

    # Result list
    panel_x1 = 795
    panel_y1 = 360
    panel_x2 = 1265
    panel_y2 = 695

    cv2.rectangle(
        canvas,
        (panel_x1, panel_y1),
        (panel_x2, panel_y2),
        (30, 30, 30),
        -1
    )

    y = 387

    for item in rois:

        name = item["name"]

        result = results[
            name
        ]["result"]

        color = (
            (0, 210, 0)
            if result == "PASS"
            else (0, 0, 255)
        )

        cv2.putText(
            canvas,
            name,
            (815, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (210, 210, 210),
            1,
            cv2.LINE_AA
        )

        cv2.putText(
            canvas,
            result,
            (1175, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43,
            color,
            1,
            cv2.LINE_AA
        )

        y += 23

    # Footer
    cv2.rectangle(
        canvas,
        (20, 535),
        (780, 695),
        (30, 30, 30),
        -1
    )

    cv2.putText(
        canvas,
        "ROBOT POSE",
        (45, 570),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_TOP / FIXED",
        (245, 570),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.56,
        (235, 235, 235),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "QC INPUT",
        (45, 610),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "D435 RGB + DEPTH SHAPE",
        (245, 610),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.56,
        (235, 235, 235),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "TOP RESULT",
        (45, 655),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (170, 170, 170),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        view_result,
        (245, 655),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        result_color,
        2,
        cv2.LINE_AA
    )

    return canvas


def build_not_evaluated(
    reason
):
    canvas = np.zeros(
        (HEIGHT, WIDTH, 3),
        dtype=np.uint8
    )

    cv2.putText(
        canvas,
        "HARMONY PRE-ROOF QUALITY INSPECTION",
        (30, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (240, 240, 240),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_TOP : NOT_EVALUATED",
        (30, 125),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (0, 200, 255),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        reason[:100],
        (30, 180),
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
            rgb = fetch("/raw")
            depth = fetch("/depthimg")

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
                    "TOP",
                "result":
                    view_result,
                "timestamp":
                    time.time(),
                "production_valid":
                    False,
                "rois":
                    results,
            }

        except Exception as e:

            reason = str(e)

            dashboard = build_not_evaluated(
                reason
            )

            status = {
                "inspection":
                    "PRE_ROOF",
                "view":
                    "TOP",
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

        ok, encoded = cv2.imencode(
            ".jpg",
            dashboard,
            [cv2.IMWRITE_JPEG_QUALITY, 90]
        )

        if ok:
            with lock:
                latest_jpeg = (
                    encoded.tobytes()
                )
                latest_status = status

        time.sleep(LOOP_DELAY)


class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        fmt,
        *args
    ):
        pass

    def do_GET(self):

        if (
            self.path == "/"
            or self.path.startswith("/?")
        ):
            body = b"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Harmony PRE-ROOF TOP QC</title>
</head>
<body style="margin:0;background:#111">
<img src="/stream"
 style="display:block;width:100%;max-width:1280px;margin:auto">
</body>
</html>
"""
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/status"):

            with lock:
                body = json.dumps(
                    latest_status,
                    ensure_ascii=False,
                    indent=2
                ).encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(len(body))
            )
            self.send_header(
                "Cache-Control",
                "no-cache"
            )
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/stream"):

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

                    with lock:
                        jpg = latest_jpeg

                    if jpg is None:
                        time.sleep(0.1)
                        continue

                    self.wfile.write(
                        b"--frame\r\n"
                    )
                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\n"
                    )
                    self.wfile.write(
                        (
                            f"Content-Length: "
                            f"{len(jpg)}\r\n\r\n"
                        ).encode()
                    )
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                    time.sleep(0.10)

            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
            ):
                pass

            return

        self.send_response(404)
        self.end_headers()


threading.Thread(
    target=worker,
    daemon=True
).start()

server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    Handler
)

print("============================================")
print(" HARMONY PRE-ROOF VIEW_TOP QC RUNTIME V5")
print("============================================")
print(f"VIEW       : TOP")
print(f"QC URL     : http://127.0.0.1:{PORT}/")
print(f"STATUS     : http://127.0.0.1:{PORT}/status")
print(f"INPUT      : {SOURCE}")
print(f"ROI COUNT  : {len(rois)}")
print("ROBOT CTRL : NONE")
print("SERVER TX  : NOT YET ENABLED")
print("UNITY TX   : QC MJPEG READY")
print("VALIDITY   : DEVELOPMENT / production_valid=false")
print("============================================")

try:
    server.serve_forever()

except KeyboardInterrupt:
    pass

finally:
    running = False
    server.server_close()
