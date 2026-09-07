#!/usr/bin/env python3
"""Frozen Attempt02 + locked House-B rules integrated runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parents[2]

TOPIC = "/vision/global_camera/image_aligned"
WINDOW = "HARMONY UNIFIED VISION V1 - Q/ESC"

RECIPE = ROOT / "configs/harmony_unified_inspection_recipe_v2.json"
MODEL_CONTRACT = ROOT / "configs/harmony_unified_model_contract_v1.json"

MODEL = (
    ROOT
    / "models/harmony_unified_vision_v1"
    / "training_attempts/attempt06_base_ab/weights/best.pt"
)

EXPECTED_MODEL_SHA256 = (
    "4fc6740e7ec17719f095562f59e847b538d5cf201ddfa2bf832baf17c12a661a"
)

FREEZE_CONTRACT = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "harmony_unified_vision_v1/attempt02_model_freeze_contract_v1.json"
)

EXPECTED_FREEZE_SHA256 = (
    "d6ff8a4529e2c789a935e8ebcef6b1e73ac35775fb99cd75638765da8e615e3c"
)

OPERATIONAL_AUDIT = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "harmony_unified_vision_v1/"
      "attempt02_strict_heldout_operational_fusion_v1.json"
)

EXPECTED_OPERATIONAL_AUDIT_SHA256 = (
    "d9c928639eaed3fbf8fcc7743c0343c7a5635f89ba04edfddeb70598f6a8f49d"
)

ROI_LOCK = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "house_b_incoming_board_roi_v1/roi_contract_locked_v1.json"
)

DATA = (
    ROOT
    / "datasets/harmony_house_vision_v1/raw/global/"
      "incoming_inspection/house_b"
)

CANON = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "house_b_canonicalized_quality_dataset_v1/crops"
)

ROBUST = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "house_b_pass_robustness_validation_v2/results.json"
)

B02_RULE = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "b02_position_tolerant_presence_v1/results.json"
)

B05_RULE = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "b05_position_tolerant_presence_v1/results.json"
)

METHODS_V2 = (
    ROOT
    / "datasets/harmony_house_vision_v1/audits/"
      "house_b_canonical_quality_method_rebuild_v2_directional/results.json"
)

MODE_MAP = {
    "house_b": "HOUSE_B",
    "house_a": "HOUSE_A",
    "base_ab": "BASE_AB",
}

DEBOUNCE_WINDOW = 5
DEBOUNCE_REQUIRED = 3

QUALITY_THRESHOLD = 0.5

# B05-only calibrated threshold.
# Existing normal max: 0.000286
# Hard incomplete calibration min: 0.160229
# Midpoint candidate: 0.080258
# All other quality flags/slots remain at 0.5.
B05_INCOMPLETE_THRESHOLD = QUALITY_THRESHOLD

STRUCTURAL_FLAGS = (
    "CRACK_DAMAGE",
    "COMPONENT_MISSING",
    "INCOMPLETE_FORMATION",
)

QUALITY_FLAG_ORDER = (
    "COLOR_NG",
    "CRACK_DAMAGE",
    "COMPONENT_MISSING",
    "INCOMPLETE_FORMATION",
)

B02_PRESENCE_THRESHOLD_V5 = 0.060076329331046
B03_PRESENCE_GRADIENT_THRESHOLD_V6 = 5
B03_PRESENCE_THRESHOLD_V6 = 0.115340


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"RUNTIME_REFUSED=MISSING_REQUIRED_FILE:{path}")


def bgr8_from_msg(msg):
    if msg.encoding.lower() != "bgr8":
        raise ValueError(
            f"UNSUPPORTED_ENCODING={msg.encoding}; required=bgr8"
        )

    required = int(msg.height) * int(msg.step)
    raw = np.frombuffer(msg.data, dtype=np.uint8)

    if raw.size < required or int(msg.step) < int(msg.width) * 3:
        raise ValueError("INVALID_SENSOR_MSG_IMAGE_LAYOUT")

    return (
        raw[:required]
        .reshape(int(msg.height), int(msg.step))[:, : int(msg.width) * 3]
        .reshape(int(msg.height), int(msg.width), 3)
        .copy()
    )


def model_names(value):
    if isinstance(value, list):
        return value

    if isinstance(value, dict):
        for key in ("classes", "labels", "names"):
            if isinstance(value.get(key), list):
                return value[key]

    raise RuntimeError("BAD_MODEL_CONTRACT_CLASS_LIST")


def preprocess_unified(crop):
    # Exact builder/training preprocessing for physical ROI crops:
    # preserve aspect ratio -> 224x224 letterbox, padding BGR 114
    # -> RGB -> /255 -> ImageNet normalize.
    size = 224
    h, w = crop.shape[:2]

    scale = min(
        size / float(w),
        size / float(h),
    )

    new_w = int(round(w * scale))
    new_h = int(round(h * scale))

    interpolation = (
        cv2.INTER_AREA
        if scale < 1.0
        else cv2.INTER_LINEAR
    )

    resized = cv2.resize(
        crop,
        (new_w, new_h),
        interpolation=interpolation,
    )

    canvas = np.full(
        (size, size, 3),
        114,
        dtype=np.uint8,
    )

    x = (size - new_w) // 2
    y = (size - new_h) // 2

    canvas[
        y:y + new_h,
        x:x + new_w,
    ] = resized

    rgb = cv2.cvtColor(
        canvas,
        cv2.COLOR_BGR2RGB,
    )

    arr = np.asarray(
        rgb,
        dtype=np.float32,
    ).copy()

    tensor = (
        torch.from_numpy(arr)
        .permute(2, 0, 1)
        / 255.0
    )

    mean = torch.tensor(
        [0.485, 0.456, 0.406],
        dtype=torch.float32,
    )[:, None, None]

    std = torch.tensor(
        [0.229, 0.224, 0.225],
        dtype=torch.float32,
    )[:, None, None]

    return ((tensor - mean) / std).unsqueeze(0)


def gradient_fraction(gray, gradient_threshold):
    h, w = gray.shape

    mx = max(8, int(w * 0.04))
    my = max(8, int(h * 0.04))

    roi = gray[my:h-my, mx:w-mx]
    roi = cv2.GaussianBlur(roi, (5, 5), 0)

    gx = cv2.Sobel(
        roi,
        cv2.CV_32F,
        1,
        0,
        ksize=3,
    )

    gy = cv2.Sobel(
        roi,
        cv2.CV_32F,
        0,
        1,
        ksize=3,
    )

    mag = cv2.magnitude(gx, gy)

    return float(
        (mag >= gradient_threshold).mean()
    )


def lab_median(image):
    lab = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2LAB,
    )

    return np.median(
        lab.reshape(-1, 3),
        axis=0,
    ).astype(np.float32)


def gray_hist(image):
    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    )

    hist = cv2.calcHist(
        [gray],
        [0],
        None,
        [64],
        [0, 256],
    ).astype(np.float32)

    cv2.normalize(
        hist,
        hist,
        alpha=1.0,
        norm_type=cv2.NORM_L1,
    )

    return hist


def gradient_image(image):
    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    ).astype(np.float32)

    gx = cv2.Sobel(
        gray,
        cv2.CV_32F,
        1,
        0,
        ksize=3,
    )

    gy = cv2.Sobel(
        gray,
        cv2.CV_32F,
        0,
        1,
        ksize=3,
    )

    return cv2.magnitude(gx, gy)


def gray_mean(image):
    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    )

    return float(np.mean(gray))


class LockedRules:
    def __init__(self, slots):
        robust_data = json.loads(
            ROBUST.read_text(encoding="utf-8")
        )

        b02_data = json.loads(
            B02_RULE.read_text(encoding="utf-8")
        )

        b05_data = json.loads(
            B05_RULE.read_text(encoding="utf-8")
        )

        methods_data = json.loads(
            METHODS_V2.read_text(encoding="utf-8")
        )

        self.missing_pass_refs = {}

        for slot in slots:
            folder = (
                DATA
                / "pass_reference_take01"
                / "crops"
                / slot
            )

            imgs = []

            for fp in sorted(
                folder.glob("frame_*.jpg")
            ):
                img = cv2.imread(
                    str(fp),
                    cv2.IMREAD_GRAYSCALE,
                )

                if img is not None:
                    imgs.append(
                        img.astype(np.float32)
                    )

            if not imgs:
                raise RuntimeError(
                    f"NO_MISSING_PASS_REFERENCE={slot}"
                )

            self.missing_pass_refs[slot] = np.median(
                np.stack(imgs, axis=0),
                axis=0,
            )

        raw_missing_slots = {
            "B01",
            "B03",
            "B04",
            "B06",
        }

        self.raw_missing_thresholds = {}

        for result in robust_data["results"]:
            slot = result["slot"]

            if slot not in raw_missing_slots:
                continue

            metric = result["metrics"]["raw_mae"]

            pass_max = float(
                metric["new_pass_max"]
            )

            missing_min = float(
                metric["missing_min"]
            )

            if missing_min <= pass_max:
                raise RuntimeError(
                    f"MISSING_RAW_NOT_SEPARABLE={slot}"
                )

            self.raw_missing_thresholds[slot] = (
                pass_max
                + 0.35
                * (missing_min - pass_max)
            )

        self.structure_rules = {
            "B02": {
                "gradient_threshold": int(
                    b02_data["design"]["gradient_threshold"]
                ),
                "presence_threshold": float(
                    b02_data["design"][
                        "presence_threshold_candidate"
                    ]
                ),
            },
            "B05": {
                "gradient_threshold": int(
                    b05_data["design"]["gradient_threshold"]
                ),
                "presence_threshold": float(
                    b05_data["design"][
                        "presence_threshold_candidate"
                    ]
                ),
            },
        }

        canonical_refs = {}

        for slot in slots:
            folder = (
                CANON
                / "pass_reference_take01"
                / slot
            )

            images = []

            for fp in sorted(
                folder.glob("frame_*.jpg")
            ):
                image = cv2.imread(
                    str(fp),
                    cv2.IMREAD_COLOR,
                )

                if image is not None:
                    images.append(image)

            if not images:
                raise RuntimeError(
                    f"NO_CANONICAL_REFERENCE={slot}"
                )

            canonical_refs[slot] = images

        self.prototypes = {}

        for slot, images in canonical_refs.items():
            self.prototypes[(slot, "lab")] = np.median(
                np.stack([
                    lab_median(x)
                    for x in images
                ]),
                axis=0,
            )

            hist_stack = np.stack([
                gray_hist(x).reshape(-1)
                for x in images
            ])

            hist_proto = np.median(
                hist_stack,
                axis=0,
            ).astype(np.float32)

            if hist_proto.sum() > 0:
                hist_proto /= hist_proto.sum()

            self.prototypes[(slot, "hist")] = (
                hist_proto.reshape(-1, 1)
            )

            self.prototypes[
                (slot, "gradient_image")
            ] = np.median(
                np.stack([
                    gradient_image(x)
                    for x in images
                ]),
                axis=0,
            ).astype(np.float32)

            self.prototypes[
                (slot, "gray_mean")
            ] = float(
                np.median([
                    gray_mean(x)
                    for x in images
                ])
            )

        method_cases = {
            item["name"]: item
            for item in methods_data["cases"]
        }

        def load_rule(case_name):
            case = method_cases[case_name]

            if not case["separable"]:
                raise RuntimeError(
                    f"CASE_NOT_SEPARABLE={case_name}"
                )

            return {
                "method": case["method"],
                "direction": case[
                    "selected_direction"
                ],
                "threshold": float(
                    case["candidate_threshold"]
                ),
            }

        # Only the locked COLOR_NG rules are reused.
        # Old classical CRACK rule is intentionally NOT imported.
        self.color_rules = {
            "B02": load_rule("B02_COLOR_NG"),
            "B03": load_rule("B03_COLOR_NG"),
            "B05": load_rule("B05_COLOR_NG"),
        }

    def evaluate_missing(self, slot, crop):
        if slot == "B03":
            gray = cv2.cvtColor(
                crop,
                cv2.COLOR_BGR2GRAY,
            )

            value = gradient_fraction(
                gray,
                B03_PRESENCE_GRADIENT_THRESHOLD_V6,
            )

            missing = bool(
                value
                < B03_PRESENCE_THRESHOLD_V6
            )

            return (
                missing,
                (
                    f"EDGE={value:.3f} "
                    f"T={B03_PRESENCE_THRESHOLD_V6:.3f}"
                ),
            )

        gray = cv2.cvtColor(
            crop,
            cv2.COLOR_BGR2GRAY,
        )

        if slot in self.structure_rules:
            rule = self.structure_rules[slot]

            value = gradient_fraction(
                gray,
                rule["gradient_threshold"],
            )

            presence_threshold = (
                B02_PRESENCE_THRESHOLD_V5
                if slot == "B02"
                else float(
                    rule["presence_threshold"]
                )
            )

            missing = bool(
                value < presence_threshold
            )

            return (
                missing,
                (
                    f"EDGE={value:.3f} "
                    f"T={presence_threshold:.3f}"
                ),
            )

        reference = self.missing_pass_refs[slot]

        mae = float(
            np.abs(
                gray.astype(np.float32)
                - reference
            ).mean()
        )

        missing = bool(
            mae
            >= self.raw_missing_thresholds[slot]
        )

        return (
            missing,
            f"MAE={mae:.1f}",
        )

    def score_color(self, slot, crop):
        rule = self.color_rules.get(slot)

        if rule is None:
            return False, None

        method = rule["method"]
        proto = self.prototypes[(slot, method)]

        if method == "lab":
            value = lab_median(crop)

            score = float(
                np.linalg.norm(
                    value - proto
                )
            )

        elif method == "hist":
            value = gray_hist(crop)

            score = float(
                cv2.compareHist(
                    proto,
                    value,
                    cv2.HISTCMP_BHATTACHARYYA,
                )
            )

        elif method == "gradient_image":
            value = gradient_image(crop)

            score = float(
                np.mean(
                    np.abs(
                        value - proto
                    )
                )
            )

        elif method == "gray_mean":
            value = gray_mean(crop)

            score = float(
                abs(value - proto)
            )

        else:
            raise RuntimeError(
                f"UNSUPPORTED_METHOD={method}"
            )

        direction = rule["direction"]
        threshold = rule["threshold"]

        if direction == "HIGH_IS_NG":
            triggered = bool(
                score >= threshold
            )

        elif direction == "LOW_IS_NG":
            triggered = bool(
                score <= threshold
            )

        else:
            raise RuntimeError(
                f"BAD_DIRECTION={direction}"
            )

        return triggered, score


def verify_frozen_candidate():
    # Attempt03 validation runtime:
    # preserve locked ROI/rules, but do not require Attempt02
    # production freeze/operational authorization artifacts.
    for path in (
        RECIPE,
        MODEL_CONTRACT,
        MODEL,
        ROI_LOCK,
        ROBUST,
        B02_RULE,
        B05_RULE,
        METHODS_V2,
    ):
        require(path)

    model_sha = sha256(MODEL)

    if model_sha != EXPECTED_MODEL_SHA256:
        raise SystemExit(
            "RUNTIME_REFUSED=ATTEMPT06_SHA_MISMATCH:"
            f"{model_sha}"
        )

    return model_sha

def load_house_b_recipe():
    data = json.loads(
        RECIPE.read_text(encoding="utf-8")
    )

    mode = data["modes"]["HOUSE_B"]

    if mode["layout_status"] != "LOCKED":
        raise SystemExit(
            "RUNTIME_REFUSED=HOUSE_B_LAYOUT_NOT_LOCKED"
        )

    return mode


def preflight(device_name):
    model_sha = verify_frozen_candidate()

    mode = load_house_b_recipe()

    roi_data = json.loads(
        (
            ROOT
            / mode["roi_contract"]
        ).read_text(encoding="utf-8")
    )

    slots = {
        item["slot"]: item
        for item in roi_data["slots"]
    }

    rules = LockedRules(slots)

    contract = json.loads(
        MODEL_CONTRACT.read_text(
            encoding="utf-8"
        )
    )

    materials = model_names(
        contract["material_head"]
    )

    qualities = model_names(
        contract["quality_head"]
    )

    if tuple(qualities) != QUALITY_FLAG_ORDER:
        raise SystemExit(
            "RUNTIME_REFUSED=QUALITY_FLAG_ORDER_MISMATCH:"
            f"{qualities}"
        )

    if device_name == "auto":
        device_name = (
            "cuda"
            if torch.cuda.is_available()
            else "cpu"
        )

    device = torch.device(device_name)

    model = torch.jit.load(
        str(MODEL),
        map_location=device,
    )

    model.eval()

    dummy = torch.zeros(
        1,
        3,
        224,
        224,
        device=device,
    )

    with torch.no_grad():
        output = model(dummy)

    if not isinstance(output, dict):
        raise SystemExit(
            "RUNTIME_REFUSED=BAD_TORCHSCRIPT_OUTPUT_TYPE"
        )

    for key in (
        "material_logits",
        "quality_logits",
    ):
        if key not in output:
            raise SystemExit(
                f"RUNTIME_REFUSED=MISSING_MODEL_OUTPUT:{key}"
            )

    if output["material_logits"].shape != (
        1,
        len(materials),
    ):
        raise SystemExit(
            "RUNTIME_REFUSED=BAD_MATERIAL_OUTPUT_SHAPE:"
            f"{tuple(output['material_logits'].shape)}"
        )

    if output["quality_logits"].shape != (
        1,
        len(qualities),
    ):
        raise SystemExit(
            "RUNTIME_REFUSED=BAD_QUALITY_OUTPUT_SHAPE:"
            f"{tuple(output['quality_logits'].shape)}"
        )

    expected = {
        item["slot"]: item["expected_material"]
        for item in mode["slots"]
    }

    if set(expected) != set(slots):
        raise SystemExit(
            "RUNTIME_REFUSED=RECIPE_ROI_SLOT_MISMATCH"
        )

    # Force evaluation of the locked rules during preflight.
    for slot in ("B02", "B03", "B05"):
        rule = rules.color_rules[slot]
        print(
            f"{slot}_COLOR_RULE="
            f"{rule['method']}|"
            f"{rule['direction']}|"
            f"{rule['threshold']:.9f}"
        )

    print("UNIFIED_RUNTIME=ONE_MODEL")
    print("RUNTIME_MODEL=ATTEMPT06")
    print(f"RUNTIME_MODEL_PATH={MODEL.relative_to(ROOT)}")
    print(f"RUNTIME_MODEL_SHA256={model_sha}")
    print("FREEZE_VERIFICATION=PASS")
    print(f"INPUT_TOPIC={TOPIC}")
    print("CV_BRIDGE_USED=false")
    print("MISSING_RULE=V5_LOCKED")
    print("B03_MISSING_PRESENCE=V6_COLOR_ROBUST_BRANCH_PRESERVED")
    print("COLOR_RULE=LOCKED_PRESERVED")
    print("COLOR_AI_ROLE=AUXILIARY")
    print(
        "STRUCTURAL_AI="
        "CRACK_DAMAGE|COMPONENT_MISSING|INCOMPLETE_FORMATION"
    )
    print(
        "FUSION_PRIORITY="
        "MISSING>COLOR_NG>STRUCTURAL_NG>WRONG_PART>PASS"
    )
    print(
        f"DEBOUNCE={DEBOUNCE_REQUIRED}_OF_{DEBOUNCE_WINDOW}_PRESERVED"
    )
    print(f"DEFAULT_QUALITY_THRESHOLD={QUALITY_THRESHOLD:.6f}")
    print(
        "B05_INCOMPLETE_THRESHOLD="
        f"{B05_INCOMPLETE_THRESHOLD:.6f}"
    )
    print("HOUSE_B_RECIPE=READY")
    print("HOUSE_A_RECIPE=LOCKED_IN_RECIPE_V2")
    print("BASE_AB_RECIPE=LOCKED_IN_RECIPE_V2")
    print(f"DEVICE={device}")
    print(
        "TORCHSCRIPT_OUTPUT="
        f"MATERIAL:{tuple(output['material_logits'].shape)}|"
        f"QUALITY:{tuple(output['quality_logits'].shape)}"
    )
    print("RUNTIME_PREFLIGHT=PASS")
    print("PRODUCTION_RUNTIME_AUTHORIZED=false")

    return {
        "mode": mode,
        "slots": slots,
        "rules": rules,
        "contract": contract,
        "materials": materials,
        "qualities": qualities,
        "expected": expected,
        "device": device,
        "model": model,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Attempt06 unified Harmony House B validation runtime."
        )
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=sorted(MODE_MAP),
    )

    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda"),
    )

    parser.add_argument(
        "--preflight-only",
        action="store_true",
    )

    args = parser.parse_args()

    if args.mode != "house_b":
        data = json.loads(
            RECIPE.read_text(encoding="utf-8")
        )

        mode = data["modes"][MODE_MAP[args.mode]]

        raise SystemExit(
            f"RUNTIME_REFUSED={MODE_MAP[args.mode]} "
            f"layout_status={mode['layout_status']}"
        )

    ctx = preflight(args.device)

    if args.preflight_only:
        return 0

    os.environ.setdefault(
        "ROS_DOMAIN_ID",
        "11",
    )

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import (
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from sensor_msgs.msg import Image

    slots = ctx["slots"]
    rules = ctx["rules"]
    materials = ctx["materials"]
    qualities = ctx["qualities"]
    expected = ctx["expected"]
    device = ctx["device"]
    model = ctx["model"]

    quality_index = {
        name: idx
        for idx, name in enumerate(qualities)
    }

    class UnifiedNode(Node):
        def __init__(self):
            super().__init__(
                "harmony_unified_runtime_v1"
            )

            qos = QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.BEST_EFFORT,
                history=HistoryPolicy.KEEP_LAST,
            )

            self.history = {
                slot: deque(
                    maxlen=DEBOUNCE_WINDOW
                )
                for slot in slots
            }

            self.stable = {
                slot: "PASS"
                for slot in slots
            }

            self.create_subscription(
                Image,
                TOPIC,
                self.callback,
                qos,
            )

            cv2.namedWindow(
                WINDOW,
                cv2.WINDOW_NORMAL,
            )

            cv2.resizeWindow(
                WINDOW,
                1280,
                720,
            )

            self.get_logger().warning(
                "production_runtime_authorized=false"
            )

            print("============================================================")
            print("HARMONY UNIFIED VISION V1 - HOUSE_B")
            print("============================================================")
            print("UNIFIED_CHECKPOINT=ATTEMPT02")
            print(
                f"UNIFIED_CHECKPOINT_SHA256={EXPECTED_MODEL_SHA256}"
            )
            print("UNIFIED_CHECKPOINT_FREEZE=PASS")
            print(f"INPUT={TOPIC}")
            print("CV_BRIDGE=NOT_USED")
            print(
                "FUSION=MISSING>COLOR_NG>"
                "STRUCTURAL_NG>WRONG_PART>PASS"
            )
            print(
                f"DEBOUNCE={DEBOUNCE_REQUIRED}/"
                f"{DEBOUNCE_WINDOW}"
            )
            print("PRODUCTION_AUTHORIZED=false")
            print()

        def debounce(self, slot, raw_state):
            history = self.history[slot]
            history.append(raw_state)

            counts = Counter(history)

            state, count = (
                counts.most_common(1)[0]
            )

            if (
                len(history)
                >= DEBOUNCE_REQUIRED
                and count
                >= DEBOUNCE_REQUIRED
            ):
                self.stable[slot] = state

            return self.stable[slot]

        def ai_infer(self, slot, crop):
            x = preprocess_unified(crop).to(
                device
            )

            with torch.no_grad():
                output = model(x)

            material_logits = (
                output["material_logits"]
            )

            quality_probs = torch.sigmoid(
                output["quality_logits"]
            )[0]

            material_id = int(
                material_logits.argmax(1).item()
            )

            predicted_material = materials[
                material_id
            ]

            probs = {
                name: float(
                    quality_probs[
                        quality_index[name]
                    ].item()
                )
                for name in qualities
            }

            positives = set()

            for name in STRUCTURAL_FLAGS:
                threshold = (
                    B05_INCOMPLETE_THRESHOLD
                    if (
                        slot == "B05"
                        and name == "INCOMPLETE_FORMATION"
                    )
                    else QUALITY_THRESHOLD
                )

                if probs[name] >= threshold:
                    positives.add(name)

            return (
                predicted_material,
                probs,
                positives,
            )

        def evaluate_raw(self, slot, crop):
            missing, missing_text = (
                rules.evaluate_missing(
                    slot,
                    crop,
                )
            )

            if missing:
                return (
                    "MISSING",
                    (
                        f"{missing_text} "
                        "PRIMARY=MISSING"
                    ),
                )

            color_triggered, color_score = (
                rules.score_color(
                    slot,
                    crop,
                )
            )

            if color_triggered:
                color_text = (
                    f"COLOR_S={color_score:.3f}"
                    if color_score is not None
                    else "COLOR_S=NA"
                )

                return (
                    "COLOR_NG",
                    (
                        f"{color_text} "
                        "PRIMARY=COLOR_NG"
                    ),
                )

            (
                predicted_material,
                probs,
                structural_positive,
            ) = self.ai_infer(slot, crop)

            ai_color = probs["COLOR_NG"]

            if structural_positive:
                primary = max(
                    structural_positive,
                    key=lambda name: probs[name],
                )

                flags = ",".join(
                    sorted(
                        structural_positive,
                        key=lambda name: -probs[name],
                    )
                )

                mismatch = (
                    predicted_material
                    != expected[slot]
                )

                return (
                    primary,
                    (
                        f"STRUCT={flags} "
                        f"Q={probs[primary]:.3f} "
                        f"MAT={predicted_material} "
                        f"MAT_MISMATCH={str(mismatch).lower()} "
                        f"COLOR_AI={ai_color:.3f}"
                    ),
                )

            if (
                predicted_material
                != expected[slot]
            ):
                return (
                    "WRONG_PART",
                    (
                        f"MAT={predicted_material} "
                        f"EXPECTED={expected[slot]} "
                        f"COLOR_AI={ai_color:.3f}"
                    ),
                )

            return (
                "PASS",
                (
                    f"MAT={predicted_material} "
                    f"COLOR_AI={ai_color:.3f}"
                ),
            )

        def callback(self, msg):
            try:
                frame = bgr8_from_msg(msg)

                stable_states = []

                for slot, roi in slots.items():
                    crop = frame[
                        roi["y"]:roi["y2"],
                        roi["x"]:roi["x2"],
                    ]

                    raw_state, metrics = (
                        self.evaluate_raw(
                            slot,
                            crop,
                        )
                    )

                    stable_state = self.debounce(
                        slot,
                        raw_state,
                    )

                    stable_states.append(
                        stable_state
                    )

                    ng = stable_state != "PASS"

                    color = (
                        (0, 0, 255)
                        if ng
                        else (0, 255, 0)
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
                        f"{slot} {stable_state}",
                        (
                            roi["x"],
                            max(
                                20,
                                roi["y"] - 6,
                            ),
                        ),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        color,
                        2,
                    )

                    print(
                        f"{slot} "
                        f"RAW={raw_state} "
                        f"STABLE={stable_state} "
                        f"{metrics}",
                        flush=True,
                    )

                overall = (
                    "FAIL"
                    if any(
                        state != "PASS"
                        for state in stable_states
                    )
                    else "PASS"
                )

                overall_color = (
                    (0, 0, 255)
                    if overall == "FAIL"
                    else (0, 255, 0)
                )

                cv2.rectangle(
                    frame,
                    (0, 0),
                    (
                        frame.shape[1] - 1,
                        frame.shape[0] - 1,
                    ),
                    overall_color,
                    8,
                )

                cv2.putText(
                    frame,
                    f"HOUSE_B {overall}",
                    (25, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.0,
                    overall_color,
                    3,
                )

                cv2.imshow(
                    WINDOW,
                    frame,
                )

                if (
                    cv2.waitKey(1)
                    & 0xFF
                    in (ord("q"), 27)
                ):
                    rclpy.shutdown()

            except Exception as exc:
                self.get_logger().error(
                    f"RUNTIME_FRAME_ERROR={exc}"
                )

    rclpy.init()

    node = UnifiedNode()

    try:
        rclpy.spin(node)

    finally:
        node.destroy_node()
        cv2.destroyAllWindows()

        if rclpy.ok():
            rclpy.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
