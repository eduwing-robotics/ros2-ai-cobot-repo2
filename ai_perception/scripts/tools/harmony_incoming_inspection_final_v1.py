#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np
import rclpy
import torch

from rclpy.node import Node
from rclpy.qos import (
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from sensor_msgs.msg import Image

import harmony_unified_runtime_attempt06_house_b_v2 as hb


ROOT = Path(__file__).resolve().parents[2]

RECIPE = (
    ROOT
    / "configs/harmony_unified_inspection_recipe_v2.json"
)

RAW_TOPIC = "/vision/global_camera/image_raw"
ALIGNED_TOPIC = "/vision/global_camera/image_aligned"

WINDOW = "HARMONY FINAL INCOMING INSPECTION V1"

MODE_KEYS = {
    ord("1"): "BASE_AB",
    ord("2"): "HOUSE_B",
    ord("3"): "HOUSE_A",
}

MODE_TOPIC = {
    "BASE_AB": RAW_TOPIC,
    "HOUSE_B": ALIGNED_TOPIC,
    "HOUSE_A": RAW_TOPIC,
}

QUALITY_ORDER = (
    "COLOR_NG",
    "CRACK_DAMAGE",
    "COMPONENT_MISSING",
    "INCOMPLETE_FORMATION",
)

DEBOUNCE_WINDOW = 5
DEBOUNCE_REQUIRED = 3

HOLD_COLOR = (0, 215, 255)
PASS_COLOR = (0, 220, 0)
FAIL_COLOR = (0, 0, 255)
INFO_COLOR = (255, 255, 255)


def load_recipe():
    if not RECIPE.is_file():
        raise SystemExit(
            f"RUNTIME_REFUSED=RECIPE_MISSING:{RECIPE}"
        )

    data = json.loads(
        RECIPE.read_text(encoding="utf-8")
    )

    if data.get(
        "production_runtime_authorized"
    ) is not False:
        raise SystemExit(
            "RUNTIME_REFUSED="
            "PRODUCTION_AUTHORIZATION_UNEXPECTED"
        )

    return data


def load_roi(mode):
    path = ROOT / mode["roi_contract"]

    if not path.is_file():
        raise SystemExit(
            f"RUNTIME_REFUSED=ROI_MISSING:{path}"
        )

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    if data.get("status") != "ROI_LOCKED":
        raise SystemExit(
            f"RUNTIME_REFUSED=ROI_NOT_LOCKED:{path}"
        )

    return {
        item["slot"]: item
        for item in data["slots"]
    }


def build_contexts(hb_ctx):
    recipe = load_recipe()

    contexts = {}

    for mode_name in (
        "BASE_AB",
        "HOUSE_B",
        "HOUSE_A",
    ):
        mode = recipe["modes"][mode_name]

        if mode["layout_status"] != "LOCKED":
            raise SystemExit(
                f"RUNTIME_REFUSED="
                f"{mode_name}_LAYOUT_NOT_LOCKED"
            )

        if (
            mode["runtime_status"]
            != "VALIDATION_RUNTIME_READY"
        ):
            raise SystemExit(
                f"RUNTIME_REFUSED="
                f"{mode_name}_NOT_RUNTIME_READY"
            )

        if mode_name == "HOUSE_B":
            slots = hb_ctx["slots"]
            expected = hb_ctx["expected"]
            rules = hb_ctx["rules"]
        else:
            slots = load_roi(mode)

            expected = {
                item["slot"]:
                    item["expected_material"]
                for item in mode["slots"]
            }

            if set(slots) != set(expected):
                raise SystemExit(
                    f"RUNTIME_REFUSED="
                    f"{mode_name}_ROI_RECIPE_MISMATCH"
                )

            rules = None

        contexts[mode_name] = {
            "mode": mode,
            "slots": slots,
            "expected": expected,
            "rules": rules,
            "topic": MODE_TOPIC[mode_name],
        }

    return contexts


class IncomingNode(Node):
    def __init__(
        self,
        hb_ctx,
        contexts,
    ):
        super().__init__(
            "harmony_incoming_inspection_final_v1"
        )

        self.model = hb_ctx["model"]
        self.device = hb_ctx["device"]
        self.materials = hb_ctx["materials"]
        self.qualities = hb_ctx["qualities"]

        self.quality_index = {
            name: idx
            for idx, name in enumerate(
                self.qualities
            )
        }

        self.contexts = contexts

        self.latest_raw = None
        self.latest_aligned = None

        self.mode_name = "BASE_AB"
        self.state = "HOLD"

        self.histories = {}
        self.result_snapshot = {}
        self.overall_result = None

        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )

        self.create_subscription(
            Image,
            RAW_TOPIC,
            self.raw_cb,
            qos,
        )

        self.create_subscription(
            Image,
            ALIGNED_TOPIC,
            self.aligned_cb,
            qos,
        )

        self.reset_histories()

        print()
        print(
            "============================================================"
        )
        print(
            "HARMONY FINAL INCOMING INSPECTION V1"
        )
        print(
            "============================================================"
        )
        print(
            "MODEL=ATTEMPT06_BASE_AB_CUMULATIVE"
        )
        print(
            f"MODEL_SHA256={hb.EXPECTED_MODEL_SHA256}"
        )
        print(
            "MODES=BASE_AB|HOUSE_B|HOUSE_A"
        )
        print(
            f"RAW_TOPIC={RAW_TOPIC}"
        )
        print(
            f"ALIGNED_TOPIC={ALIGNED_TOPIC}"
        )
        print(
            "HOUSE_B_FUSION="
            "MISSING>COLOR_NG>"
            "STRUCTURAL_NG>WRONG_PART>PASS"
        )
        print(
            f"DEBOUNCE="
            f"{DEBOUNCE_REQUIRED}_OF_"
            f"{DEBOUNCE_WINDOW}"
        )
        print(
            "SERVER_TRANSMIT=false"
        )
        print(
            "UNITY_VIDEO_TRANSMIT=false"
        )
        print(
            "PRODUCTION_RUNTIME_AUTHORIZED=false"
        )
        print()
        self.print_controls()

    def print_controls(self):
        print(
            "[1] BASE_AB  "
            "[2] HOUSE_B  "
            "[3] HOUSE_A  "
            "[H] HOLD  "
            "[SPACE/ENTER] INSPECT  "
            "[R] RESET  "
            "[Q/ESC] QUIT"
        )

    def raw_cb(self, msg):
        try:
            self.latest_raw = hb.bgr8_from_msg(
                msg
            )
        except Exception as exc:
            self.get_logger().error(
                f"RAW_FRAME_ERROR={exc}"
            )

    def aligned_cb(self, msg):
        try:
            self.latest_aligned = (
                hb.bgr8_from_msg(msg)
            )
        except Exception as exc:
            self.get_logger().error(
                f"ALIGNED_FRAME_ERROR={exc}"
            )

    def context(self):
        return self.contexts[
            self.mode_name
        ]

    def current_frame(self):
        if self.mode_name == "HOUSE_B":
            return self.latest_aligned

        return self.latest_raw

    def reset_histories(self):
        slots = self.contexts[
            self.mode_name
        ]["slots"]

        self.histories = {
            slot: deque(
                maxlen=DEBOUNCE_WINDOW
            )
            for slot in slots
        }

        self.result_snapshot = {}
        self.overall_result = None

    def set_mode(self, mode_name):
        if self.mode_name == mode_name:
            return

        self.mode_name = mode_name
        self.state = "HOLD"
        self.reset_histories()

        print()
        print(
            f"MODE_CHANGED={mode_name}"
        )
        print(
            f"INPUT_TOPIC="
            f"{self.context()['topic']}"
        )
        print(
            "STATE=HOLD"
        )

    def hold(self):
        self.state = "HOLD"
        self.reset_histories()

        print()
        print(
            f"MODE={self.mode_name}"
        )
        print(
            "STATE=HOLD"
        )
        print(
            "RESULT_COMMIT=false"
        )

    def start_inspection(self):
        self.reset_histories()
        self.state = "INSPECTING"

        print()
        print(
            f"MODE={self.mode_name}"
        )
        print(
            "STATE=INSPECTING"
        )
        print(
            "RESULT_COMMIT_PENDING=true"
        )

    def reset_current(self):
        self.state = "HOLD"
        self.reset_histories()

        print()
        print(
            f"MODE={self.mode_name}"
        )
        print(
            "STATE=RESET_TO_HOLD"
        )

    def infer(self, crop):
        x = hb.preprocess_unified(
            crop
        ).to(self.device)

        with torch.no_grad():
            output = self.model(x)

        material_logits = (
            output["material_logits"]
        )

        quality_probs = torch.sigmoid(
            output["quality_logits"]
        )[0]

        material_id = int(
            material_logits.argmax(
                1
            ).item()
        )

        predicted = self.materials[
            material_id
        ]

        material_conf = float(
            torch.softmax(
                material_logits,
                dim=1,
            )[0, material_id].item()
        )

        probs = {
            name: float(
                quality_probs[
                    self.quality_index[name]
                ].item()
            )
            for name in self.qualities
        }

        return (
            predicted,
            material_conf,
            probs,
        )

    def evaluate_generic(
        self,
        slot,
        crop,
    ):
        expected = self.context()[
            "expected"
        ][slot]

        (
            predicted,
            material_conf,
            probs,
        ) = self.infer(crop)

        defects = [
            name
            for name in QUALITY_ORDER
            if probs[name] >= 0.5
        ]

        defects.sort(
            key=lambda name: -probs[name]
        )

        material_match = (
            predicted == expected
        )

        if defects:
            raw_state = defects[0]
            result = "FAIL"
        elif not material_match:
            raw_state = "WRONG_PART"
            result = "FAIL"
        else:
            raw_state = "PASS"
            result = "PASS"

        return {
            "slot_id": slot,
            "expected_class_name":
                expected,
            "predicted_class_name":
                predicted,
            "material_confidence":
                material_conf,
            "material_status": (
                "MATCH"
                if material_match
                else "MISMATCH"
            ),
            "raw_state": raw_state,
            "result": result,
            "defects": defects,
            "quality_scores": {
                name: probs[name]
                for name in QUALITY_ORDER
            },
            "metrics": (
                f"MAT={predicted} "
                f"CONF={material_conf:.3f}"
            ),
        }

    def evaluate_house_b(
        self,
        slot,
        crop,
    ):
        ctx = self.context()
        rules = ctx["rules"]
        expected = ctx[
            "expected"
        ][slot]

        (
            missing,
            missing_text,
        ) = rules.evaluate_missing(
            slot,
            crop,
        )

        if missing:
            return {
                "slot_id": slot,
                "expected_class_name":
                    expected,
                "predicted_class_name":
                    None,
                "material_confidence":
                    None,
                "material_status":
                    "NOT_EVALUATED",
                "raw_state":
                    "MISSING",
                "result":
                    "FAIL",
                "defects": [
                    "COMPONENT_MISSING"
                ],
                "quality_scores": {},
                "metrics": (
                    f"{missing_text} "
                    "PRIMARY=MISSING"
                ),
            }

        (
            color_triggered,
            color_score,
        ) = rules.score_color(
            slot,
            crop,
        )

        if color_triggered:
            return {
                "slot_id": slot,
                "expected_class_name":
                    expected,
                "predicted_class_name":
                    None,
                "material_confidence":
                    None,
                "material_status":
                    "NOT_EVALUATED",
                "raw_state":
                    "COLOR_NG",
                "result":
                    "FAIL",
                "defects": [
                    "COLOR_NG"
                ],
                "quality_scores": {},
                "metrics": (
                    "COLOR_S="
                    + (
                        f"{color_score:.3f}"
                        if color_score
                        is not None
                        else "NA"
                    )
                ),
            }

        (
            predicted,
            material_conf,
            probs,
        ) = self.infer(crop)

        structural = []

        for name in hb.STRUCTURAL_FLAGS:
            threshold = (
                hb.B05_INCOMPLETE_THRESHOLD
                if (
                    slot == "B05"
                    and name
                    == "INCOMPLETE_FORMATION"
                )
                else hb.QUALITY_THRESHOLD
            )

            if probs[name] >= threshold:
                structural.append(name)

        structural.sort(
            key=lambda name: -probs[name]
        )

        material_match = (
            predicted == expected
        )

        if structural:
            raw_state = structural[0]
            result = "FAIL"
            defects = structural

        elif not material_match:
            raw_state = "WRONG_PART"
            result = "FAIL"
            defects = []

        else:
            raw_state = "PASS"
            result = "PASS"
            defects = []

        return {
            "slot_id": slot,
            "expected_class_name":
                expected,
            "predicted_class_name":
                predicted,
            "material_confidence":
                material_conf,
            "material_status": (
                "MATCH"
                if material_match
                else "MISMATCH"
            ),
            "raw_state": raw_state,
            "result": result,
            "defects": defects,
            "quality_scores": {
                name: probs[name]
                for name in QUALITY_ORDER
            },
            "metrics": (
                f"MAT={predicted} "
                f"CONF={material_conf:.3f} "
                f"COLOR_AI="
                f"{probs['COLOR_NG']:.3f}"
            ),
        }

    def evaluate_slot(
        self,
        slot,
        crop,
    ):
        if self.mode_name == "HOUSE_B":
            return self.evaluate_house_b(
                slot,
                crop,
            )

        return self.evaluate_generic(
            slot,
            crop,
        )

    def stable_candidate(
        self,
        slot,
    ):
        history = self.histories[slot]

        if len(history) < (
            DEBOUNCE_REQUIRED
        ):
            return None

        counts = Counter(
            item["raw_state"]
            for item in history
        )

        state, count = (
            counts.most_common(1)[0]
        )

        if count < DEBOUNCE_REQUIRED:
            return None

        for item in reversed(history):
            if item["raw_state"] == state:
                return dict(item)

        return None

    def inspect_once(
        self,
        frame,
    ):
        ctx = self.context()

        for slot, roi in (
            ctx["slots"].items()
        ):
            crop = frame[
                roi["y"]:roi["y2"],
                roi["x"]:roi["x2"],
            ]

            result = self.evaluate_slot(
                slot,
                crop,
            )

            self.histories[slot].append(
                result
            )

        candidates = {
            slot:
                self.stable_candidate(
                    slot
                )
            for slot in ctx["slots"]
        }

        if not all(
            x is not None
            for x in candidates.values()
        ):
            return

        self.result_snapshot = {
            slot: candidates[slot]
            for slot in ctx["slots"]
        }

        self.overall_result = (
            "FAIL"
            if any(
                item["result"] != "PASS"
                for item
                in self.result_snapshot.values()
            )
            else "PASS"
        )

        self.state = "RESULT"

        self.emit_result_console()

    def emit_result_console(self):
        payload = {
            "ver": "0.2-preview",
            "inspection_type":
                "INCOMING_QA",
            "inspection_mode":
                self.mode_name,
            "result":
                self.overall_result,
            "items": [
                self.result_snapshot[slot]
                for slot in sorted(
                    self.result_snapshot
                )
            ],
        }

        print()
        print(
            "============================================================"
        )
        print(
            "INCOMING_INSPECTION_RESULT_LOCKED"
        )
        print(
            "============================================================"
        )
        print(
            "RESULT_JSON="
            + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        print(
            "SERVER_TRANSMIT=false"
        )
        print(
            "STATE=RESULT"
        )
        print()

    def draw_hold(
        self,
        frame,
    ):
        ctx = self.context()

        for slot, roi in (
            ctx["slots"].items()
        ):
            cv2.rectangle(
                frame,
                (roi["x"], roi["y"]),
                (roi["x2"], roi["y2"]),
                HOLD_COLOR,
                2,
            )

            cv2.putText(
                frame,
                f"{slot} HOLD",
                (
                    roi["x"] + 5,
                    max(
                        22,
                        roi["y"] + 22,
                    ),
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                HOLD_COLOR,
                2,
                cv2.LINE_AA,
            )

    def draw_inspecting(
        self,
        frame,
    ):
        ctx = self.context()

        for slot, roi in (
            ctx["slots"].items()
        ):
            candidate = (
                self.stable_candidate(
                    slot
                )
            )

            if candidate is None:
                label = (
                    f"{slot} INSPECTING"
                )
                color = HOLD_COLOR
            else:
                label = (
                    f"{slot} "
                    f"{candidate['raw_state']}"
                )
                color = (
                    PASS_COLOR
                    if candidate["result"]
                    == "PASS"
                    else FAIL_COLOR
                )

            cv2.rectangle(
                frame,
                (roi["x"], roi["y"]),
                (roi["x2"], roi["y2"]),
                color,
                2,
            )

            cv2.putText(
                frame,
                label,
                (
                    roi["x"] + 5,
                    max(
                        22,
                        roi["y"] + 22,
                    ),
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                cv2.LINE_AA,
            )

    def draw_result(
        self,
        frame,
    ):
        ctx = self.context()

        for slot, roi in (
            ctx["slots"].items()
        ):
            item = (
                self.result_snapshot[
                    slot
                ]
            )

            color = (
                PASS_COLOR
                if item["result"] == "PASS"
                else FAIL_COLOR
            )

            cv2.rectangle(
                frame,
                (roi["x"], roi["y"]),
                (roi["x2"], roi["y2"]),
                color,
                3,
            )

            line1 = (
                f"{slot} "
                f"{item['result']}"
            )

            predicted = (
                item[
                    "predicted_class_name"
                ]
                or "-"
            )

            line2 = (
                f"MAT={predicted}"
            )

            if item["defects"]:
                line3 = (
                    "NG="
                    + ",".join(
                        item["defects"]
                    )
                )
            elif (
                item["material_status"]
                == "MISMATCH"
            ):
                line3 = (
                    "WRONG_PART"
                )
            else:
                line3 = "QUALITY=OK"

            x = roi["x"] + 5
            y = max(
                24,
                roi["y"] + 24,
            )

            cv2.putText(
                frame,
                line1,
                (x, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                line2,
                (x, y + 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                color,
                1,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                line3,
                (x, y + 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                color,
                1,
                cv2.LINE_AA,
            )

    def compose_canvas(
        self,
        frame,
    ):
        canvas = np.zeros(
            (840, 1280, 3),
            dtype=np.uint8,
        )

        canvas[70:790, :, :] = frame

        if self.overall_result is None:
            overall = "-"
            overall_color = INFO_COLOR
        else:
            overall = self.overall_result
            overall_color = (
                PASS_COLOR
                if overall == "PASS"
                else FAIL_COLOR
            )

        cv2.putText(
            canvas,
            "HARMONY INCOMING INSPECTION",
            (18, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            INFO_COLOR,
            2,
            cv2.LINE_AA,
        )

        cv2.putText(
            canvas,
            (
                f"MODE={self.mode_name}   "
                f"STATE={self.state}   "
                f"OVERALL={overall}"
            ),
            (18, 56),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            overall_color,
            2,
            cv2.LINE_AA,
        )

        controls = (
            "[1] BASE_AB  "
            "[2] HOUSE_B  "
            "[3] HOUSE_A   "
            "[H] HOLD   "
            "[SPACE/ENTER] INSPECT   "
            "[R] RESET   "
            "[Q] QUIT"
        )

        cv2.putText(
            canvas,
            controls,
            (230, 822),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            INFO_COLOR,
            1,
            cv2.LINE_AA,
        )

        return canvas

    def render(self):
        source = self.current_frame()

        if source is None:
            frame = np.zeros(
                (720, 1280, 3),
                dtype=np.uint8,
            )

            topic = self.context()[
                "topic"
            ]

            cv2.putText(
                frame,
                "WAITING FOR CAMERA FRAME",
                (350, 330),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                FAIL_COLOR,
                2,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                topic,
                (330, 375),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                INFO_COLOR,
                2,
                cv2.LINE_AA,
            )

        else:
            frame = source.copy()

            if frame.shape[:2] != (
                720,
                1280,
            ):
                frame = cv2.resize(
                    frame,
                    (1280, 720),
                )

            if self.state == "HOLD":
                self.draw_hold(frame)

            elif self.state == "INSPECTING":
                self.inspect_once(
                    frame
                )

                if self.state == "RESULT":
                    self.draw_result(
                        frame
                    )
                else:
                    self.draw_inspecting(
                        frame
                    )

            elif self.state == "RESULT":
                self.draw_result(
                    frame
                )

        return self.compose_canvas(
            frame
        )


def main():
    os.environ.setdefault(
        "ROS_DOMAIN_ID",
        "11",
    )

    print(
        "===== ATTEMPT06 SHARED MODEL PREFLIGHT ====="
    )

    hb_ctx = hb.preflight(
        "cuda"
        if torch.cuda.is_available()
        else "cpu"
    )

    contexts = build_contexts(
        hb_ctx
    )

    print()
    print(
        "FINAL_INCOMING_PREFLIGHT=PASS"
    )
    print(
        "DEFAULT_MODE=BASE_AB"
    )
    print(
        "DEFAULT_STATE=HOLD"
    )
    print(
        "PRODUCTION_RUNTIME_AUTHORIZED=false"
    )

    rclpy.init()

    node = IncomingNode(
        hb_ctx,
        contexts,
    )

    cv2.namedWindow(
        WINDOW,
        cv2.WINDOW_NORMAL,
    )

    cv2.resizeWindow(
        WINDOW,
        1280,
        840,
    )

    try:
        while rclpy.ok():
            rclpy.spin_once(
                node,
                timeout_sec=0.02,
            )

            canvas = node.render()

            cv2.imshow(
                WINDOW,
                canvas,
            )

            key = cv2.waitKey(1) & 0xFF

            if key in MODE_KEYS:
                node.set_mode(
                    MODE_KEYS[key]
                )

            elif key in (
                ord("h"),
                ord("H"),
            ):
                node.hold()

            elif key in (
                ord(" "),
                13,
            ):
                node.start_inspection()

            elif key in (
                ord("r"),
                ord("R"),
            ):
                node.reset_current()

            elif key in (
                ord("q"),
                ord("Q"),
                27,
            ):
                break

    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()

        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
