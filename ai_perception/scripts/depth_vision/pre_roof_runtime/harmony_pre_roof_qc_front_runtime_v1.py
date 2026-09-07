from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.request
import json
import time

import cv2
import numpy as np


ROOT = Path.home() / "vision_project"

BASE = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc"
)

SOURCE = "http://192.168.20.10:8766"
RGB_URL = SOURCE + "/raw"
DEPTH_URL = SOURCE + "/depthimg"

PORT = 8787

GOLDEN_RGB_PATH = (
    BASE /
    "view_front_golden_v1/golden_rgb_median.png"
)

GOLDEN_DEPTH_PATH = (
    BASE /
    "view_front_golden_v1/golden_depth_median.png"
)

ROI_PATH = (
    BASE /
    "view_front_roi_v2.json"
)

THRESHOLD_PATH = (
    BASE /
    "view_front_thresholds_v1.json"
)


gold_rgb = cv2.imread(
    str(GOLDEN_RGB_PATH)
)

gold_depth = cv2.imread(
    str(GOLDEN_DEPTH_PATH)
)

if gold_rgb is None:
    raise SystemExit(
        f"ABORT: missing RGB Golden: {GOLDEN_RGB_PATH}"
    )

if gold_depth is None:
    raise SystemExit(
        f"ABORT: missing Depth Golden: {GOLDEN_DEPTH_PATH}"
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

ROIS = roi_doc["rois"]
THRESHOLDS = threshold_doc["thresholds"]

ENABLED_METRICS = (
    "mean_absdiff",
    "p95_absdiff",
    "changed_fraction_20",
)

lock = threading.Lock()

latest_status = {
    "schema":
        "harmony_pre_roof_view_front_runtime_v1",

    "inspection":
        "PRE_ROOF",

    "view":
        "FRONT",

    "result":
        "NOT_EVALUATED",

    "runtime":
        "V1",

    "runtime_port":
        PORT,

    "robot_pose":
        "VIEW_FRONT_FIXED",

    "transform":
        "ROTATE_180",

    "golden":
        "V1",

    "roi":
        "V2",

    "threshold":
        "V1",

    "rois":
        {},

    "production_valid":
        False,
}

latest_dashboard = None


def fetch(url):

    with urllib.request.urlopen(
        url,
        timeout=4
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
            f"JPEG decode failed: {url}"
        )

    return cv2.rotate(
        img,
        cv2.ROTATE_180
    )


def edge_map(img):

    gray = cv2.cvtColor(
        img,
        cv2.COLOR_BGR2GRAY
    )

    return cv2.Canny(
        gray,
        80,
        160
    )


def compute_metrics(current, golden):

    diff = cv2.absdiff(
        current,
        golden
    )

    gray_diff = cv2.cvtColor(
        diff,
        cv2.COLOR_BGR2GRAY
    )

    e1 = edge_map(current)
    e2 = edge_map(golden)

    return {
        "mean_absdiff":
            float(np.mean(diff)),

        "p95_absdiff":
            float(np.percentile(diff, 95)),

        "changed_fraction_20":
            float(np.mean(gray_diff >= 20)),

        "edge_change_fraction":
            float(
                np.mean(
                    cv2.absdiff(e1, e2) > 0
                )
            ),
    }


def evaluate_source(metrics, thresholds):

    exceeded = {}

    for name in ENABLED_METRICS:

        exceeded[name] = (
            metrics[name]
            > thresholds[name]
        )

    exceed_count = sum(
        1
        for v in exceeded.values()
        if v
    )

    result = (
        "FAIL"
        if exceed_count >= 2
        else "PASS"
    )

    return {
        "result":
            result,

        "exceed_count":
            exceed_count,

        "metrics":
            metrics,

        "exceeded":
            exceeded,

        "thresholds":
            thresholds,
    }


def evaluate_roi(
    name,
    current_rgb,
    current_depth,
    reference_rgb,
    reference_depth,
):

    rgb_metrics = compute_metrics(
        current_rgb,
        reference_rgb
    )

    depth_metrics = compute_metrics(
        current_depth,
        reference_depth
    )

    rgb_eval = evaluate_source(
        rgb_metrics,
        THRESHOLDS[name]["rgb"]
    )

    depth_eval = evaluate_source(
        depth_metrics,
        THRESHOLDS[name]["depth"]
    )

    roi_fail = (
        rgb_eval["result"] == "FAIL"
        or depth_eval["result"] == "FAIL"
    )

    return {
        "result":
            "FAIL" if roi_fail else "PASS",

        "reason":
            (
                "NORMAL_REFERENCE_DEVIATION"
                if roi_fail
                else "WITHIN_REFERENCE_RANGE"
            ),

        "rgb":
            rgb_eval,

        "depth":
            depth_eval,
    }


def draw_roi_overlay(
    rgb,
    roi_results,
):

    out = rgb.copy()

    for r in ROIS:

        name = r["name"]

        x = r["x"]
        y = r["y"]
        w = r["w"]
        h = r["h"]

        result = (
            roi_results
            .get(name, {})
            .get("result", "NOT_EVALUATED")
        )

        if result == "PASS":
            color = (0, 255, 0)

        elif result == "FAIL":
            color = (0, 0, 255)

        else:
            color = (0, 255, 255)

        thickness = (
            3
            if name == "FRONT_BODY"
            else 2
        )

        cv2.rectangle(
            out,
            (x, y),
            (x + w, y + h),
            color,
            thickness
        )

        cv2.putText(
            out,
            name,
            (x + 6, y + 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA
        )

    return out


def make_dashboard(
    rgb,
    depth,
    status,
):

    canvas = np.zeros(
        (720, 1280, 3),
        dtype=np.uint8
    )

    result = status["result"]

    if result == "PASS":
        result_color = (0, 255, 0)

    elif result == "FAIL":
        result_color = (0, 0, 255)

    else:
        result_color = (0, 255, 255)


    cv2.putText(
        canvas,
        "HARMONY PRE-ROOF QUALITY INSPECTION",
        (55, 55),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_FRONT / ROTATE_180",
        (55, 85),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (180, 180, 180),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        f"VIEW_FRONT : {result}",
        (900, 60),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        result_color,
        2,
        cv2.LINE_AA
    )


    rgb_overlay = draw_roi_overlay(
        rgb,
        status["rois"]
    )

    rgb_show = cv2.resize(
        rgb_overlay,
        (690, 388)
    )

    dep_show = cv2.resize(
        depth,
        (440, 248),
        interpolation=cv2.INTER_NEAREST
    )


    cv2.putText(
        canvas,
        "RGB QC + ROI / ROT 180",
        (55, 120),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (200, 200, 200),
        1,
        cv2.LINE_AA
    )

    canvas[
        135:523,
        55:745
    ] = rgb_show


    cv2.putText(
        canvas,
        "DEPTH SHAPE / ROT 180",
        (790, 120),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (200, 200, 200),
        1,
        cv2.LINE_AA
    )

    canvas[
        135:383,
        790:1230
    ] = dep_show


    y = 420

    for name in (
        "FRONT_BODY",
        "UPPER_REGION",
        "MIDDLE_REGION",
        "LOWER_REGION",
    ):

        rr = status["rois"].get(
            name,
            {}
        )

        r = rr.get(
            "result",
            "NOT_EVALUATED"
        )

        if r == "PASS":
            c = (0, 255, 0)

        elif r == "FAIL":
            c = (0, 0, 255)

        else:
            c = (0, 255, 255)

        cv2.putText(
            canvas,
            name,
            (810, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.53,
            (200, 200, 200),
            1,
            cv2.LINE_AA
        )

        cv2.putText(
            canvas,
            r,
            (1110, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.53,
            c,
            2,
            cv2.LINE_AA
        )

        y += 32


    cv2.putText(
        canvas,
        "ROBOT POSE",
        (70, 575),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (160, 160, 160),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "VIEW_FRONT / FIXED",
        (250, 575),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )


    cv2.putText(
        canvas,
        "QC INPUT",
        (70, 615),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (160, 160, 160),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        "D435 RGB + DEPTH SHAPE",
        (250, 615),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA
    )


    cv2.putText(
        canvas,
        "FRONT RESULT",
        (70, 655),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (160, 160, 160),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        canvas,
        result,
        (250, 655),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        result_color,
        2,
        cv2.LINE_AA
    )


    cv2.putText(
        canvas,
        "DEVELOPMENT / production_valid=false",
        (790, 655),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (180, 180, 180),
        1,
        cv2.LINE_AA
    )

    return canvas


def worker():

    global latest_status
    global latest_dashboard

    while True:

        try:

            rgb = fetch(RGB_URL)
            depth = fetch(DEPTH_URL)

            if depth.shape[:2] != rgb.shape[:2]:

                depth = cv2.resize(
                    depth,
                    (
                        rgb.shape[1],
                        rgb.shape[0]
                    ),
                    interpolation=cv2.INTER_NEAREST
                )


            roi_results = {}

            for r in ROIS:

                name = r["name"]

                x = r["x"]
                y = r["y"]
                w = r["w"]
                h = r["h"]

                roi_results[name] = evaluate_roi(
                    name,

                    rgb[
                        y:y+h,
                        x:x+w
                    ],

                    depth[
                        y:y+h,
                        x:x+w
                    ],

                    gold_rgb[
                        y:y+h,
                        x:x+w
                    ],

                    gold_depth[
                        y:y+h,
                        x:x+w
                    ],
                )


            view_fail = any(
                x["result"] == "FAIL"
                for x in roi_results.values()
            )

            result = (
                "FAIL"
                if view_fail
                else "PASS"
            )


            status = {
                "schema":
                    "harmony_pre_roof_view_front_runtime_v1",

                "inspection":
                    "PRE_ROOF",

                "view":
                    "FRONT",

                "result":
                    result,

                "runtime":
                    "V1",

                "runtime_port":
                    PORT,

                "robot_pose":
                    "VIEW_FRONT_FIXED",

                "transform":
                    "ROTATE_180",

                "source":
                    SOURCE,

                "golden":
                    "V1",

                "roi":
                    "V2",

                "threshold":
                    "V1",

                "decision_policy":
                    "NORMAL_REFERENCE_DEVIATION",

                "rois":
                    roi_results,

                "production_valid":
                    False,
            }


            dashboard = make_dashboard(
                rgb,
                depth,
                status
            )


            with lock:

                latest_status = status
                latest_dashboard = dashboard


        except Exception as e:

            status = {
                "schema":
                    "harmony_pre_roof_view_front_runtime_v1",

                "inspection":
                    "PRE_ROOF",

                "view":
                    "FRONT",

                "result":
                    "NOT_EVALUATED",

                "runtime":
                    "V1",

                "runtime_port":
                    PORT,

                "error":
                    str(e),

                "production_valid":
                    False,
            }

            with lock:
                latest_status = status

        time.sleep(0.15)


class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        fmt,
        *args
    ):
        return


    def send_bytes(
        self,
        body,
        content_type
    ):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        try:
            self.wfile.write(body)

        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            pass


    def get_dashboard_jpeg(self):

        with lock:
            img = (
                latest_dashboard.copy()
                if latest_dashboard is not None
                else None
            )

        if img is None:

            img = np.zeros(
                (720, 1280, 3),
                dtype=np.uint8
            )

            cv2.putText(
                img,
                "VIEW_FRONT / NOT_EVALUATED",
                (350, 350),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (0, 255, 255),
                2,
                cv2.LINE_AA
            )

        ok, buf = cv2.imencode(
            ".jpg",
            img,
            [
                int(
                    cv2.IMWRITE_JPEG_QUALITY
                ),
                90
            ]
        )

        if not ok:
            raise RuntimeError(
                "JPEG encode failed"
            )

        return buf.tobytes()


    def do_GET(self):

        if self.path.startswith(
            "/status"
        ):

            with lock:
                status = json.loads(
                    json.dumps(
                        latest_status
                    )
                )

            body = json.dumps(
                status,
                indent=2
            ).encode()

            self.send_bytes(
                body,
                "application/json"
            )

            return


        if self.path.startswith(
            "/raw"
        ):

            body = (
                self.get_dashboard_jpeg()
            )

            self.send_bytes(
                body,
                "image/jpeg"
            )

            return


        if self.path.startswith(
            "/stream"
        ):

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

            while True:

                try:

                    jpg = (
                        self.get_dashboard_jpeg()
                    )

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

                    self.wfile.write(
                        jpg
                    )

                    self.wfile.write(
                        b"\r\n"
                    )

                    self.wfile.flush()

                    time.sleep(0.20)

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                ):
                    break

            return


        html = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Harmony PRE-ROOF FRONT QC</title>

<style>

html, body {
    margin: 0;
    padding: 0;
    background: #1b1b1b;
}

.wrap {
    width: 1280px;
    margin: 20px auto;
}

img {
    width: 1280px;
    height: 720px;
    display: block;
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

        self.send_bytes(
            html.encode(),
            "text/html; charset=utf-8"
        )


threading.Thread(
    target=worker,
    daemon=True
).start()


print()
print("============================================")
print(" HARMONY PRE-ROOF VIEW_FRONT QC RUNTIME V1")
print("============================================")
print("VIEW       : FRONT")
print(f"QC URL     : http://127.0.0.1:{PORT}/")
print(f"STATUS     : http://127.0.0.1:{PORT}/status")
print("INPUT      :", SOURCE)
print("TRANSFORM  : ROTATE_180")
print("ROI COUNT  :", len(ROIS))
print("GOLDEN     : V1")
print("ROI        : V2")
print("THRESHOLD  : V1")
print("ROBOT CTRL : NONE")
print("SERVER TX  : NOT YET ENABLED")
print("UNITY TX   : QC MJPEG READY")
print("VALIDITY   : DEVELOPMENT / production_valid=false")
print("============================================")


server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    Handler
)

server.daemon_threads = True

try:
    server.serve_forever()

except KeyboardInterrupt:
    pass

finally:
    server.server_close()
