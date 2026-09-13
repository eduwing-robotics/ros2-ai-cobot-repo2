from pathlib import Path
import json
import time

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSProfile,
    QoSReliabilityPolicy,
    QoSHistoryPolicy,
)
from sensor_msgs.msg import Image


ROOT = Path.home() / "vision_project"

REFERENCE = (
    ROOT
    / "datasets/harmony_house_vision_v1"
    / "audits/house_b_global_camera_recovery_lock_v1"
    / "recovered_reference_1280x720.png"
)

ROI_FILE = (
    ROOT
    / "datasets/harmony_house_vision_v1"
    / "audits/house_b_incoming_board_roi_v1"
    / "roi_contract_locked_v1.json"
)

INPUT_TOPIC = "/vision/global_camera/image_raw"
OUTPUT_TOPIC = "/vision/global_camera/image_aligned"

SCALE = 0.5

MAX_SHIFT_PX = 60.0
MAX_ROTATION_DEG = 3.0
MIN_ECC = 0.65


if not REFERENCE.exists():
    raise SystemExit(
        f"ABORT: reference missing: {REFERENCE}"
    )

if not ROI_FILE.exists():
    raise SystemExit(
        f"ABORT: ROI contract missing: {ROI_FILE}"
    )


reference_bgr = cv2.imread(
    str(REFERENCE),
    cv2.IMREAD_COLOR,
)

if reference_bgr is None:
    raise SystemExit(
        f"ABORT: failed to read: {REFERENCE}"
    )

if reference_bgr.shape[:2] != (720, 1280):
    raise SystemExit(
        f"ABORT: bad reference shape={reference_bgr.shape}"
    )

reference_gray = cv2.cvtColor(
    reference_bgr,
    cv2.COLOR_BGR2GRAY,
)

roi_data = json.loads(
    ROI_FILE.read_text(
        encoding="utf-8"
    )
)

slots = roi_data["slots"]


# ============================================================
# Alignment mask
#
# Material interiors are ignored.
# Tape/board/background remain as alignment anchors.
# ============================================================

mask = np.full(
    (720, 1280),
    255,
    dtype=np.uint8,
)

for s in slots:

    x1 = int(s["x"])
    y1 = int(s["y"])
    x2 = int(s["x2"])
    y2 = int(s["y2"])

    margin = 8

    xa = min(
        x2,
        x1 + margin,
    )

    ya = min(
        y2,
        y1 + margin,
    )

    xb = max(
        x1,
        x2 - margin,
    )

    yb = max(
        y1,
        y2 - margin,
    )

    if xb > xa and yb > ya:
        mask[
            ya:yb,
            xa:xb,
        ] = 0


small_size = (
    int(1280 * SCALE),
    int(720 * SCALE),
)

ref_small = cv2.resize(
    reference_gray,
    small_size,
    interpolation=cv2.INTER_AREA,
).astype(np.float32) / 255.0

mask_small = cv2.resize(
    mask,
    small_size,
    interpolation=cv2.INTER_NEAREST,
)


def msg_to_bgr8(msg):

    if msg.encoding.lower() != "bgr8":
        raise RuntimeError(
            f"UNSUPPORTED_ENCODING={msg.encoding}"
        )

    if (
        int(msg.width) != 1280
        or int(msg.height) != 720
    ):
        raise RuntimeError(
            f"UNEXPECTED_SIZE={msg.width}x{msg.height}"
        )

    row_bytes = int(msg.width) * 3
    required = int(msg.height) * int(msg.step)

    raw = np.frombuffer(
        msg.data,
        dtype=np.uint8,
    )

    if raw.size < required:
        raise RuntimeError(
            f"SHORT_BUFFER={raw.size}/{required}"
        )

    rows = raw[:required].reshape(
        int(msg.height),
        int(msg.step),
    )

    return rows[
        :,
        :row_bytes,
    ].reshape(
        720,
        1280,
        3,
    ).copy()


def bgr8_to_msg(
    bgr,
    source_msg,
):

    out = Image()

    out.header = source_msg.header

    out.height = 720
    out.width = 1280

    out.encoding = "bgr8"
    out.is_bigendian = 0

    out.step = 1280 * 3
    out.data = bgr.tobytes()

    return out


class AutoAlignNode(Node):

    def __init__(self):

        super().__init__(
            "house_b_global_auto_align_live_v1"
        )

        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )

        self.publisher = self.create_publisher(
            Image,
            OUTPUT_TOPIC,
            qos,
        )

        self.create_subscription(
            Image,
            INPUT_TOPIC,
            self.callback,
            qos,
        )

        self.warp_small = np.eye(
            2,
            3,
            dtype=np.float32,
        )

        self.frame_count = 0
        self.accepted = 0
        self.rejected = 0

        self.last_print = 0.0

        print(
            "============================================================"
        )
        print(
            "HOUSE_B GLOBAL AUTO ALIGN HEADLESS V1"
        )
        print(
            "============================================================"
        )
        print(
            f"INPUT={INPUT_TOPIC}"
        )
        print(
            f"OUTPUT={OUTPUT_TOPIC}"
        )
        print(
            f"REFERENCE={REFERENCE}"
        )
        print(
            "PART_ROIS_EXCLUDED_FROM_ALIGNMENT=true"
        )
        print(
            f"MAX_SHIFT_PX={MAX_SHIFT_PX}"
        )
        print(
            f"MAX_ROTATION_DEG={MAX_ROTATION_DEG}"
        )
        print(
            f"MIN_ECC={MIN_ECC}"
        )
        print()
        print(
            "No manual camera alignment is required "
            "while AUTO_ALIGN=PASS."
        )
        print()

    def callback(self, msg):

        try:
            live = msg_to_bgr8(
                msg
            )

        except Exception as e:
            print(
                f"FRAME_ERROR={e}",
                flush=True,
            )
            return

        live_gray = cv2.cvtColor(
            live,
            cv2.COLOR_BGR2GRAY,
        )

        live_small = cv2.resize(
            live_gray,
            small_size,
            interpolation=cv2.INTER_AREA,
        ).astype(np.float32) / 255.0

        warp = self.warp_small.copy()

        try:

            cc, warp = cv2.findTransformECC(
                ref_small,
                live_small,
                warp,
                cv2.MOTION_EUCLIDEAN,
                (
                    cv2.TERM_CRITERIA_EPS
                    | cv2.TERM_CRITERIA_COUNT,
                    80,
                    1e-6,
                ),
                inputMask=mask_small,
                gaussFiltSize=5,
            )

        except cv2.error:

            self.rejected += 1
            self.warp_small = np.eye(
                2,
                3,
                dtype=np.float32,
            )

            return

        a = float(
            warp[0, 0]
        )

        c = float(
            warp[1, 0]
        )

        dx = float(
            warp[0, 2]
            / SCALE
        )

        dy = float(
            warp[1, 2]
            / SCALE
        )

        shift = float(
            np.hypot(
                dx,
                dy,
            )
        )

        rotation = float(
            np.degrees(
                np.arctan2(
                    c,
                    a,
                )
            )
        )

        valid = bool(
            shift <= MAX_SHIFT_PX
            and abs(rotation) <= MAX_ROTATION_DEG
            and float(cc) >= MIN_ECC
        )

        if not valid:

            self.rejected += 1

            self.warp_small = np.eye(
                2,
                3,
                dtype=np.float32,
            )

            now = time.monotonic()

            if now - self.last_print > 1.0:

                print(
                    "AUTO_ALIGN=RECHECK "
                    f"SHIFT={shift:.2f}px "
                    f"ROT={rotation:+.3f}deg "
                    f"ECC={cc:.5f}",
                    flush=True,
                )

                self.last_print = now

            return

        self.warp_small = warp

        warp_full = warp.copy()

        warp_full[
            :,
            2
        ] /= SCALE

        aligned = cv2.warpAffine(
            live,
            warp_full,
            (1280, 720),
            flags=(
                cv2.INTER_LINEAR
                | cv2.WARP_INVERSE_MAP
            ),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(
                255,
                255,
                255,
            ),
        )

        out_msg = bgr8_to_msg(
            aligned,
            msg,
        )

        self.publisher.publish(
            out_msg
        )

        self.accepted += 1
        self.frame_count += 1

        now = time.monotonic()

        if now - self.last_print > 1.0:

            print(
                "AUTO_ALIGN=PASS "
                f"SHIFT={shift:.2f}px "
                f"DX={dx:+.2f}px "
                f"DY={dy:+.2f}px "
                f"ROT={rotation:+.3f}deg "
                f"ECC={cc:.5f}",
                flush=True,
            )

            self.last_print = now


rclpy.init()

node = AutoAlignNode()

try:
    rclpy.spin(
        node
    )

except KeyboardInterrupt:
    pass

finally:
    accepted = node.accepted
    rejected = node.rejected

    node.destroy_node()

    rclpy.shutdown()



print()
print(
    "============================================================"
)
print(
    "AUTO ALIGN STOPPED"
)
print(
    "============================================================"
)
print(
    f"ACCEPTED_FRAMES={accepted}"
)
print(
    f"REJECTED_FRAMES={rejected}"
)
print(
    f"ALIGNED_TOPIC={OUTPUT_TOPIC}"
)
print(
    "NEXT=VALIDATE_ALIGNED_TOPIC_AND_BUILD_LIVE_V5"
)
