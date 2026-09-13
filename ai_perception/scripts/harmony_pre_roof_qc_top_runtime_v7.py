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

PORT = 8775
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
    "view_top_thresholds_v4c_wall4_holdout_candidate.json"
)

GOLDEN_RGB = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_golden_v4_current/golden_rgb_median.png"
)

GOLDEN_DEPTH = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_golden_v4_current/golden_depth_median.png"
)


BASE_HOLE_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_base_holes_v1/base_holes_config.json"
)

BASE_HOLE_THRESH_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_base_holes_thresholds_v3_reinsert_final.json"
)


STRUCTURE_GALLERY_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_structure_gallery_v1b.json"
)


HOLE_PRESENCE_CONFIG = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc/"
    "view_top_hole_presence_v1.json"
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




# =========================================================
# V7 STRUCTURE GALLERY
#
# PRE_ROOF purpose:
# - component presence
# - gross position
# - gross orientation
# - missing component
# - major misassembly
#
# NOT used for assembly ROI decision:
# - absolute brightness
# - absolute RGB color
# - minor cosmetic difference
# =========================================================

structure_cfg = json.loads(
    STRUCTURE_GALLERY_CONFIG.read_text(
        encoding="utf-8"
    )
)

structure_thresholds = (
    structure_cfg["thresholds"]
)

STRUCTURE_SEARCH_PX = int(
    structure_cfg[
        "policy"
    ][
        "search_tolerance_px"
    ]
)

PRE_ROOF_ROOT = (
    ROOT /
    "datasets/harmony_parts_v1/pre_roof_qc"
)

structure_gallery = []

for rel in structure_cfg["references"]:

    p = PRE_ROOF_ROOT / rel

    img = cv2.imread(
        str(p)
    )

    if img is None:
        raise SystemExit(
            f"Missing structure gallery image: {p}"
        )

    if img.shape[:2] != (
        HEIGHT,
        WIDTH
    ):
        raise SystemExit(
            f"Bad structure gallery shape: "
            f"{p}: {img.shape}"
        )

    structure_gallery.append(
        {
            "name": rel,
            "image": img,
        }
    )


def structure_gradient(image):

    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY
    )

    gray = cv2.GaussianBlur(
        gray,
        (5, 5),
        0
    )

    gx = cv2.Sobel(
        gray,
        cv2.CV_32F,
        1,
        0,
        ksize=3
    )

    gy = cv2.Sobel(
        gray,
        cv2.CV_32F,
        0,
        1,
        ksize=3
    )

    return cv2.magnitude(
        gx,
        gy
    )


def structure_corr(a, b):

    a = a.astype(
        np.float32
    )

    b = b.astype(
        np.float32
    )

    a = a - a.mean()
    b = b - b.mean()

    sa = float(
        a.std()
    )

    sb = float(
        b.std()
    )

    if (
        sa < 1e-6
        or sb < 1e-6
    ):
        return 0.0

    return float(
        np.mean(
            (a / sa)
            *
            (b / sb)
        )
    )


def structure_best_match(
    current,
    item
):

    x = int(
        item["x"]
    )

    y = int(
        item["y"]
    )

    w = int(
        item["w"]
    )

    h = int(
        item["h"]
    )

    # FAST V7:
    # Compute current-frame gradient once instead of
    # recalculating Gaussian/Sobel for every search shift.
    current_grad_full = structure_gradient(
        current
    )

    # Assembly QC position tolerance:
    # COLUMN_2 is allowed a slightly wider positional shift.
    search_px = (
        15
        if item.get("name") == "COLUMN_2"
        else STRUCTURE_SEARCH_PX
    )

    best = None

    for ref in structure_gallery:

        ref_roi = ref[
            "image"
        ][
            y:y+h,
            x:x+w
        ]

        ref_grad = structure_gradient(
            ref_roi
        )

        for dy in range(
            -search_px,
            search_px + 1
        ):
            for dx in range(
                -search_px,
                search_px + 1
            ):

                xx = x + dx
                yy = y + dy

                if (
                    xx < 0
                    or yy < 0
                    or xx + w > current.shape[1]
                    or yy + h > current.shape[0]
                ):
                    continue

                cur_grad = current_grad_full[
                    yy:yy+h,
                    xx:xx+w
                ]

                score = structure_corr(
                    cur_grad,
                    ref_grad
                )

                candidate = {
                    "score":
                        score,

                    "reference":
                        ref["name"],

                    "shift_x":
                        dx,

                    "shift_y":
                        dy,
                }

                if (
                    best is None
                    or score > best["score"]
                ):
                    best = candidate

    if best is None:
        raise RuntimeError(
            f"No structure match: "
            f"{item['name']}"
        )

    return best




# =========================================================
# V7 HOLE PRESENCE V1
#
# PRE_ROOF hole policy:
# - silver/magnet visible => PASS
# - black/missing/covered => FAIL
#
# Absolute Golden brightness difference is NOT used.
# =========================================================

hole_presence_cfg = json.loads(
    HOLE_PRESENCE_CONFIG.read_text(
        encoding="utf-8"
    )
)


def evaluate_hole_presence(
    image,
    hole
):

    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY
    )

    base_x = int(hole["x"])
    base_y = int(hole["y"])
    w = int(hole["w"])
    h = int(hole["h"])

    margin = int(
        hole_presence_cfg[
            "local_margin_px"
        ]
    )

    delta = float(
        hole_presence_cfg[
            "bright_delta_from_local_background"
        ]
    )

    min_fraction = float(
        hole_presence_cfg[
            "silver_fraction_min"
        ]
    )

    search_px = int(
        hole_presence_cfg.get(
            "search_tolerance_px",
            0
        )
    )

    best = None

    for dy in range(
        -search_px,
        search_px + 1
    ):
        for dx in range(
            -search_px,
            search_px + 1
        ):

            x = base_x + dx
            y = base_y + dy

            if (
                x < 0
                or y < 0
                or x + w > gray.shape[1]
                or y + h > gray.shape[0]
            ):
                continue

            roi = gray[
                y:y+h,
                x:x+w
            ]

            x1 = max(
                0,
                x - margin
            )

            y1 = max(
                0,
                y - margin
            )

            x2 = min(
                gray.shape[1],
                x + w + margin
            )

            y2 = min(
                gray.shape[0],
                y + h + margin
            )

            local = gray[
                y1:y2,
                x1:x2
            ]

            mask = np.ones(
                local.shape,
                dtype=bool
            )

            rx = x - x1
            ry = y - y1

            mask[
                ry:ry+h,
                rx:rx+w
            ] = False

            background = local[
                mask
            ]

            if background.size == 0:
                continue

            bg_median = float(
                np.median(
                    background
                )
            )

            silver_threshold = (
                bg_median +
                delta
            )

            silver_fraction = float(
                np.mean(
                    roi >= silver_threshold
                )
            )

            c95_contrast = float(
                np.percentile(
                    roi,
                    95
                )
                -
                bg_median
            )

            candidate = {
                "silver_fraction":
                    silver_fraction,

                "background":
                    bg_median,

                "silver_threshold":
                    silver_threshold,

                "c95_contrast":
                    c95_contrast,

                "shift_x":
                    dx,

                "shift_y":
                    dy,
            }

            if best is None:
                best = candidate
                continue

            # Primary: strongest real silver presence.
            # Tie-break: prefer smaller movement.
            cand_key = (
                candidate[
                    "silver_fraction"
                ],
                candidate[
                    "c95_contrast"
                ],
                -(
                    abs(dx)
                    +
                    abs(dy)
                ),
            )

            best_key = (
                best[
                    "silver_fraction"
                ],
                best[
                    "c95_contrast"
                ],
                -(
                    abs(
                        best["shift_x"]
                    )
                    +
                    abs(
                        best["shift_y"]
                    )
                ),
            )

            if cand_key > best_key:
                best = candidate

    if best is None:
        raise RuntimeError(
            f"No valid search region for "
            f"{hole['name']}"
        )

    silver_fraction = float(
        best[
            "silver_fraction"
        ]
    )

    hole_fail = (
        silver_fraction
        <
        min_fraction
    )

    detail = {
        "silver_fraction": {
            "value":
                silver_fraction,

            "threshold":
                min_fraction,

            "below_threshold":
                hole_fail,
        },

        "local_background_median": {
            "value":
                best[
                    "background"
                ],
        },

        "silver_pixel_threshold": {
            "value":
                best[
                    "silver_threshold"
                ],
        },

        "c95_local_contrast": {
            "value":
                best[
                    "c95_contrast"
                ],
        },

        "best_shift_x": {
            "value":
                best[
                    "shift_x"
                ],
        },

        "best_shift_y": {
            "value":
                best[
                    "shift_y"
                ],
        },

        "search_tolerance_px": {
            "value":
                search_px,
        },

        "inspection_mode":
            "LOCAL_SILVER_PRESENCE",

        "golden_brightness_used":
            False,
    }

    return (
        hole_fail,
        detail
    )

def evaluate(rgb, depth):

    results = {}

    # -----------------------------------------------------
    # Assembly ROIs
    #
    # V7:
    # RGB absolute brightness/color metrics are NOT used.
    # Depth pixel absdiff is NOT used here.
    #
    # PASS if current structure matches at least one
    # validated normal assembly reference.
    # -----------------------------------------------------

    for item in rois:

        name = item["name"]

        # -------------------------------------------------
        # BASE_HOLES large ROI:
        #
        # V7 intentionally disables the old large-area
        # pixel-difference verdict.
        #
        # Individual hole inspection below remains active
        # for this first V7 prototype.
        # -------------------------------------------------

        if name == "BASE_HOLES":

            results[name] = {
                "group":
                    item["group"],

                "result":
                    "PASS",

                "rgb_fail":
                    False,

                "depth_fail":
                    False,

                "rgb_metric_fail_count":
                    0,

                "depth_metric_fail_count":
                    0,

                "rgb": {},

                "depth": {},

                "inspection_mode":
                    "INDIVIDUAL_HOLES_ONLY",

                "large_roi_enabled":
                    False,
            }

            continue


        if name not in structure_thresholds:
            raise RuntimeError(
                f"Missing structure threshold: "
                f"{name}"
            )


        best = structure_best_match(
            rgb,
            item
        )

        threshold = float(
            structure_thresholds[
                name
            ]
        )

        roi_fail = (
            best["score"]
            <
            threshold
        )


        results[name] = {
            "group":
                item["group"],

            "result":
                "FAIL"
                if roi_fail
                else "PASS",

            # Compatibility with existing Dashboard/status
            "rgb_fail":
                roi_fail,

            "depth_fail":
                False,

            "rgb_metric_fail_count":
                1 if roi_fail else 0,

            "depth_metric_fail_count":
                0,

            "rgb": {
                "structure_best_match": {
                    "value":
                        best["score"],

                    "threshold":
                        threshold,

                    "below_threshold":
                        roi_fail,

                    "reference":
                        best["reference"],

                    "shift_x":
                        best["shift_x"],

                    "shift_y":
                        best["shift_y"],
                }
            },

            "depth": {},

            "inspection_mode":
                "NORMAL_STRUCTURE_GALLERY",

            "absolute_brightness_used":
                False,

            "absolute_color_used":
                False,
        }


    # -----------------------------------------------------
    # Individual BASE HOLE inspection
    #
    # V7 prototype:
    # keep existing validated individual-hole logic only.
    # Large BASE_HOLES ROI pixel-diff verdict is disabled.
    # -----------------------------------------------------

    hole_results = {}

    for hole in base_holes:

        hole_name = hole["name"]

        x = int(hole["x"])
        y = int(hole["y"])
        w = int(hole["w"])
        h = int(hole["h"])

        (
            hole_fail,
            detail
        ) = evaluate_hole_presence(
            rgb,
            hole
        )

        hole_results[
            hole_name
        ] = {
            "result":
                "FAIL"
                if hole_fail
                else "PASS",

            # Compatibility field.
            "rgb_metric_fail_count":
                1 if hole_fail else 0,

            "rgb":
                detail,

            "inspection_mode":
                "LOCAL_SILVER_PRESENCE",

            "roi": {
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            }
        }


    hole_fail_count = sum(
        1
        for item
        in hole_results.values()
        if item["result"] == "FAIL"
    )


    results["BASE_HOLES"][
        "large_roi_result"
    ] = "DISABLED_V7"


    results["BASE_HOLES"][
        "holes"
    ] = hole_results


    results["BASE_HOLES"][
        "hole_fail_count"
    ] = hole_fail_count


    # V7:
    # ONLY individual-hole failures can fail BASE_HOLES.
    if hole_fail_count > 0:

        results["BASE_HOLES"][
            "result"
        ] = "FAIL"


    view_result = (
        "PASS"
        if all(
            x["result"] == "PASS"
            for x
            in results.values()
        )
        else "FAIL"
    )


    return (
        view_result,
        results
    )



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
 style="display:block;width:100%;height:auto;margin:0">
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
print(" HARMONY PRE-ROOF VIEW_TOP QC RUNTIME V6")
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
