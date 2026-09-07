#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V4 UI.

UI palette-only layer over verified Final Incoming Runtime V3.

LOCKED:
- Inspection logic unchanged
- ROI geometry unchanged
- Pixel coordinates unchanged
- Canvas geometry unchanged
- Server integration unchanged
- Unity integration unchanged
- ROS2 topics unchanged
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT
    / "scripts/tools/"
      "harmony_incoming_inspection_final_v3.py"
)


spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v3_base",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import V3 runtime: {BASE_RUNTIME}"
    )

v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)


# ============================================================
# UI PALETTE ONLY
#
# OpenCV uses BGR order.
#
# No ROI / geometry / font position / inspection parameter
# is changed in this file.
# ============================================================

v1 = v3.v2.v1

# PRE-ROOF-inspired industrial palette.
# BGR order. Visual-only values.
v1.HOLD_COLOR = (48, 148, 210)     # Industrial Amber
v1.PASS_COLOR = (88, 185, 92)      # Industrial Green
v1.FAIL_COLOR = (90, 90, 225)      # Industrial Red
v1.INFO_COLOR = (230, 230, 230)    # High-contrast Cool White


# ------------------------------------------------------------
# UI FONT VISIBILITY ONLY
#
# Keep:
# - text coordinates unchanged
# - font scales unchanged
# - ROI coordinates unchanged
# - rectangle geometry unchanged
#
# Only make thin UI text strokes easier to read.
# ------------------------------------------------------------

_ORIGINAL_PUT_TEXT = v1.cv2.putText


def _ui_put_text(
    img,
    text,
    org,
    fontFace,
    fontScale,
    color,
    thickness=1,
    lineType=None,
    bottomLeftOrigin=False,
):
    # Display text only.
    # Actual keyboard handling remains inherited unchanged.
    if (
        isinstance(text, str)
        and text.startswith("[1] BASE_AB")
        and "[SPACE/ENTER] INSPECT" in text
    ):
        text = (
            "[1] BASE A/B   "
            "[2] HOUSE B   "
            "[3] HOUSE A   |   "
            "[H] HOLD   "
            "[SPACE] INSPECT   "
            "[R] RESET   "
            "[Q] EXIT"
        )

    # Text-only readability palette.
    # Rectangle / ROI colors remain unchanged.
    text_color = color

    try:
        source_color = tuple(int(v) for v in color)
    except Exception:
        source_color = color

    if source_color == tuple(v1.HOLD_COLOR):
        text_color = (70, 190, 255)      # Bright Amber text
    elif source_color == tuple(v1.PASS_COLOR):
        text_color = (125, 225, 125)     # Bright Green text
    elif source_color == tuple(v1.FAIL_COLOR):
        text_color = (115, 115, 255)     # Bright Red text
    elif source_color == tuple(v1.INFO_COLOR):
        text_color = (245, 245, 245)     # High-contrast White

    # Small text becomes clearer with a single-pixel stroke.
    # Large labels remain thickness 2.
    if float(fontScale) < 0.48:
        ui_thickness = 1
    else:
        ui_thickness = max(
            2,
            int(thickness),
        )

    if lineType is None:
        lineType = v1.cv2.LINE_AA

    return _ORIGINAL_PUT_TEXT(
        img,
        text,
        org,
        fontFace,
        fontScale,
        text_color,
        ui_thickness,
        lineType,
        bottomLeftOrigin,
    )


v1.cv2.putText = _ui_put_text


print()
print("============================================================")
print("HARMONY FINAL INCOMING INSPECTION V4 UI")
print("============================================================")
print("BASE_RUNTIME=FINAL_V3_UNCHANGED")
print("UI_PATCH=PALETTE_ONLY")
print("ROI_GEOMETRY_CHANGE=0")
print("PIXEL_POSITION_CHANGE=0")
print("INSPECTION_LOGIC_CHANGE=0")
print("SERVER_CHANGE=0")
print("UNITY_CHANGE=0")
print("PRODUCTION_RUNTIME_AUTHORIZED=false")
print("============================================================")
print()


def main():
    return v3.main()


if __name__ == "__main__":
    raise SystemExit(main())
