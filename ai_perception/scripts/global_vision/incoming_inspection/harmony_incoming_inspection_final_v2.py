#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V2.

Server integration layer over the LOCKED Final V1 runtime.

Important:
- Final V1 is not modified.
- Attempt06 / ROI / House-B LockedRules are inherited unchanged.
- UI geometry is inherited unchanged from Final V1.
- Manual full-slot inspection remains available.
- Server v0.2 requests inspect only requested items[] slots.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from std_msgs.msg import String


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT
    / "scripts/tools/"
      "harmony_incoming_inspection_final_v1.py"
)

LOCKED_BASE_SHA256 = (
    "b3d8920bfbe5fd322c486c666825f83f"
    "0190241193c6ea733d351ca7725aa894"
)

REQUEST_TOPIC = "/vision/incoming_qa/request_v02"
RESULT_TOPIC = "/vision/incoming_qa/result_v02"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(
            lambda: stream.read(1024 * 1024),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


actual_sha = sha256_file(BASE_RUNTIME)

if actual_sha != LOCKED_BASE_SHA256:
    raise SystemExit(
        "LOCKED_V1_SHA_MISMATCH "
        f"expected={LOCKED_BASE_SHA256} "
        f"actual={actual_sha}"
    )


spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v1_locked",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import locked runtime: {BASE_RUNTIME}"
    )

v1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v1)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class IncomingNodeV2(v1.IncomingNode):
    def __init__(
        self,
        hb_ctx,
        contexts,
    ):
        super().__init__(
            hb_ctx,
            contexts,
        )

        self.active_server_request = None

        self.result_pub = self.create_publisher(
            String,
            RESULT_TOPIC,
            10,
        )

        self.request_sub = self.create_subscription(
            String,
            REQUEST_TOPIC,
            self.server_request_cb,
            10,
        )

        print()
        print(
            "============================================================"
        )
        print(
            "HARMONY FINAL INCOMING INSPECTION V2"
        )
        print(
            "============================================================"
        )
        print(
            f"LOCKED_V1_SHA256={actual_sha}"
        )
        print(
            "UI_SOURCE=LOCKED_FINAL_V1"
        )
        print(
            f"SERVER_REQUEST_TOPIC={REQUEST_TOPIC}"
        )
        print(
            f"SERVER_RESULT_TOPIC={RESULT_TOPIC}"
        )
        print(
            "SERVER_TRANSACTION_MODE=ENABLED"
        )
        print(
            "PRODUCTION_RUNTIME_AUTHORIZED=false"
        )

    def requested_slots(self):
        if self.active_server_request is None:
            return list(
                self.context()["slots"].keys()
            )

        return [
            item["slot_id"]
            for item
            in self.active_server_request["items"]
        ]

    def is_requested_slot(
        self,
        slot,
    ):
        return (
            slot
            in set(
                self.requested_slots()
            )
        )

    def transaction_locked(self):
        return (
            self.active_server_request is not None
            and self.state != "RESULT"
        )

    def clear_completed_transaction(self):
        if (
            self.active_server_request is not None
            and self.state == "RESULT"
        ):
            old_id = (
                self.active_server_request[
                    "inspection_request_id"
                ]
            )

            self.active_server_request = None

            print(
                f"SERVER_TRANSACTION_RELEASED={old_id}"
            )

    def set_mode(
        self,
        mode_name,
    ):
        if self.transaction_locked():
            print(
                "MANUAL_CONTROL_BLOCKED="
                "SERVER_TRANSACTION_ACTIVE"
            )
            return

        self.clear_completed_transaction()

        super().set_mode(
            mode_name
        )

    def hold(self):
        if self.transaction_locked():
            print(
                "MANUAL_CONTROL_BLOCKED="
                "SERVER_TRANSACTION_ACTIVE"
            )
            return

        self.clear_completed_transaction()

        super().hold()

    def start_inspection(self):
        if self.active_server_request is not None:
            print(
                "MANUAL_CONTROL_BLOCKED="
                "SERVER_TRANSACTION_ACTIVE"
            )
            return

        super().start_inspection()

    def reset_current(self):
        if self.transaction_locked():
            print(
                "MANUAL_CONTROL_BLOCKED="
                "SERVER_TRANSACTION_ACTIVE"
            )
            return

        self.clear_completed_transaction()

        super().reset_current()

    def server_request_cb(
        self,
        msg,
    ):
        try:
            payload = json.loads(
                msg.data
            )
        except json.JSONDecodeError as exc:
            print(
                f"SERVER_REQUEST_REJECTED="
                f"BAD_JSON:{exc}",
                flush=True,
            )
            return

        required = {
            "ver",
            "message_type",
            "inspection_request_id",
            "inspection_cycle",
            "inspection_mode",
            "items",
        }

        missing = sorted(
            required - set(payload)
        )

        if missing:
            print(
                "SERVER_REQUEST_REJECTED="
                "MISSING_FIELDS:"
                + ",".join(missing),
                flush=True,
            )
            return

        if (
            payload["ver"] != "0.2"
            or payload["message_type"]
            != "incoming_qa_request"
        ):
            print(
                "SERVER_REQUEST_REJECTED="
                "BAD_CONTRACT_VERSION_OR_TYPE",
                flush=True,
            )
            return

        mode = payload[
            "inspection_mode"
        ]

        if mode not in self.contexts:
            print(
                "SERVER_REQUEST_REJECTED="
                f"BAD_MODE:{mode}",
                flush=True,
            )
            return

        items = payload.get(
            "items"
        )

        if not isinstance(
            items,
            list,
        ) or not items:
            print(
                "SERVER_REQUEST_REJECTED="
                "EMPTY_ITEMS",
                flush=True,
            )
            return

        slot_ids = []

        for item in items:
            slot = item.get(
                "slot_id"
            )

            if (
                slot
                not in self.contexts[
                    mode
                ]["slots"]
            ):
                print(
                    "SERVER_REQUEST_REJECTED="
                    f"BAD_SLOT:{mode}:{slot}",
                    flush=True,
                )
                return

            if slot in slot_ids:
                print(
                    "SERVER_REQUEST_REJECTED="
                    f"DUPLICATE_SLOT:{slot}",
                    flush=True,
                )
                return

            slot_ids.append(
                slot
            )

        if self.transaction_locked():
            current = (
                self.active_server_request[
                    "inspection_request_id"
                ]
            )

            incoming = payload[
                "inspection_request_id"
            ]

            print(
                "SERVER_REQUEST_REJECTED="
                f"BUSY:current={current}:"
                f"incoming={incoming}",
                flush=True,
            )
            return

        if (
            self.active_server_request is not None
            and self.state == "RESULT"
        ):
            self.clear_completed_transaction()

        self.mode_name = mode
        self.active_server_request = payload

        self.reset_histories()
        self.state = "INSPECTING"

        print()
        print(
            "============================================================"
        )
        print(
            "SERVER_INCOMING_QA_REQUEST_ACTIVE"
        )
        print(
            "============================================================"
        )
        print(
            "INSPECTION_REQUEST_ID="
            + str(
                payload[
                    "inspection_request_id"
                ]
            )
        )
        print(
            "INSPECTION_CYCLE="
            + str(
                payload[
                    "inspection_cycle"
                ]
            )
        )
        print(
            f"MODE={mode}"
        )
        print(
            "REQUESTED_SLOTS="
            + ",".join(slot_ids)
        )
        print(
            "STATE=INSPECTING"
        )
        print(
            "RESULT_COMMIT_PENDING=true"
        )

    def inspect_once(
        self,
        frame,
    ):
        ctx = self.context()

        active_slots = (
            self.requested_slots()
        )

        for slot in active_slots:
            roi = ctx["slots"][slot]

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
            for slot in active_slots
        }

        if not all(
            x is not None
            for x in candidates.values()
        ):
            return

        self.result_snapshot = {
            slot: candidates[slot]
            for slot in active_slots
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

    def build_server_result(
        self,
    ):
        request = (
            self.active_server_request
        )

        request_items = {
            item["slot_id"]: item
            for item in request["items"]
        }

        result_items = []

        for requested in request["items"]:
            slot = requested[
                "slot_id"
            ]

            observed = (
                self.result_snapshot[
                    slot
                ]
            )

            defects = [
                name
                for name in v1.QUALITY_ORDER
                if name
                in observed["defects"]
            ]

            predicted_class_name = (
                observed[
                    "predicted_class_name"
                ]
            )

            detected_quantity = (
                1
                if predicted_class_name
                is not None
                else 0
            )

            expected_quantity = (
                requested[
                    "expected_quantity"
                ]
            )

            failure_reasons = []

            if observed["result"] == "FAIL":
                if detected_quantity == 0:
                    failure_reasons.append(
                        "MISSING"
                    )
                elif (
                    detected_quantity
                    != expected_quantity
                ):
                    failure_reasons.append(
                        "QUANTITY_MISMATCH"
                    )

                if (
                    predicted_class_name
                    is not None
                    and predicted_class_name
                    != requested[
                        "expected_class_name"
                    ]
                ):
                    failure_reasons.append(
                        "WRONG_CLASS"
                    )

                if defects:
                    failure_reasons.append(
                        "DEFECT"
                    )

            if len(failure_reasons) > 1:
                failure_type = (
                    "MULTIPLE_FAILURE"
                )
            elif len(failure_reasons) == 1:
                failure_type = (
                    failure_reasons[0]
                )
            else:
                failure_type = None

            result_items.append(
                {
                    "slot_id":
                        slot,
                    "delivery_item_id":
                        requested[
                            "delivery_item_id"
                        ],
                    "expected_part_code":
                        requested[
                            "expected_part_code"
                        ],
                    "expected_class_name":
                        requested[
                            "expected_class_name"
                        ],
                    "expected_quantity":
                        expected_quantity,
                    "detected_quantity":
                        detected_quantity,
                    "predicted_class_name":
                        predicted_class_name,
                    "material_confidence":
                        observed[
                            "material_confidence"
                        ],
                    "result":
                        observed[
                            "result"
                        ],
                    "failure_type":
                        failure_type,
                    "defects":
                        defects,
                    "quality_scores":
                        {
                            name:
                                observed[
                                    "quality_scores"
                                ][name]
                            for name
                            in v1.QUALITY_ORDER
                        },
                }
            )

        return {
            "ver": "0.2",
            "message_type":
                "incoming_qa_result",
            "inspection_request_id":
                request[
                    "inspection_request_id"
                ],
            "inspection_cycle":
                request[
                    "inspection_cycle"
                ],
            "inspection_mode":
                request[
                    "inspection_mode"
                ],
            "status":
                "COMPLETED",
            "result":
                self.overall_result,
            "items":
                result_items,
            "camera_source":
                "GLOBAL_CAMERA",
            "timestamp":
                utc_now(),
            "model_scope":
                "ATTEMPT06_BASE_AB_CUMULATIVE",
            "model_version":
                v1.hb.EXPECTED_MODEL_SHA256,
            "production_valid":
                False,
        }

    def emit_result_console(
        self,
    ):
        if self.active_server_request is None:
            super().emit_result_console()
            return

        payload = (
            self.build_server_result()
        )

        message = String()

        message.data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        self.result_pub.publish(
            message
        )

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
            + message.data
        )
        print(
            "SERVER_TRANSMIT="
            "QUEUED_TO_UDP_GATEWAY"
        )
        print(
            "STATE=RESULT"
        )
        print()

    def draw_not_requested(
        self,
        frame,
        slot,
        roi,
    ):
        color = v1.INFO_COLOR

        v1.cv2.rectangle(
            frame,
            (roi["x"], roi["y"]),
            (roi["x2"], roi["y2"]),
            color,
            2,
        )

        v1.cv2.putText(
            frame,
            f"{slot} NOT_REQUESTED",
            (
                roi["x"] + 5,
                max(
                    22,
                    roi["y"] + 22,
                ),
            ),
            v1.cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            v1.cv2.LINE_AA,
        )

    def draw_inspecting(
        self,
        frame,
    ):
        ctx = self.context()
        requested = set(
            self.requested_slots()
        )

        for slot, roi in (
            ctx["slots"].items()
        ):
            if slot not in requested:
                self.draw_not_requested(
                    frame,
                    slot,
                    roi,
                )
                continue

            candidate = (
                self.stable_candidate(
                    slot
                )
            )

            if candidate is None:
                label = (
                    f"{slot} INSPECTING"
                )
                color = v1.HOLD_COLOR
            else:
                label = (
                    f"{slot} "
                    f"{candidate['raw_state']}"
                )

                color = (
                    v1.PASS_COLOR
                    if candidate["result"]
                    == "PASS"
                    else v1.FAIL_COLOR
                )

            v1.cv2.rectangle(
                frame,
                (roi["x"], roi["y"]),
                (roi["x2"], roi["y2"]),
                color,
                2,
            )

            v1.cv2.putText(
                frame,
                label,
                (
                    roi["x"] + 5,
                    max(
                        22,
                        roi["y"] + 22,
                    ),
                ),
                v1.cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                v1.cv2.LINE_AA,
            )

    def draw_result(
        self,
        frame,
    ):
        ctx = self.context()
        requested = set(
            self.requested_slots()
        )

        for slot, roi in (
            ctx["slots"].items()
        ):
            if slot not in requested:
                self.draw_not_requested(
                    frame,
                    slot,
                    roi,
                )
                continue

            item = (
                self.result_snapshot[
                    slot
                ]
            )

            color = (
                v1.PASS_COLOR
                if item["result"]
                == "PASS"
                else v1.FAIL_COLOR
            )

            v1.cv2.rectangle(
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
                ordered = [
                    name
                    for name
                    in v1.QUALITY_ORDER
                    if name
                    in item["defects"]
                ]

                line3 = (
                    "NG="
                    + ",".join(
                        ordered
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
                line3 = (
                    "QUALITY=OK"
                )

            x = roi["x"] + 5
            y = max(
                24,
                roi["y"] + 24,
            )

            v1.cv2.putText(
                frame,
                line1,
                (x, y),
                v1.cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                v1.cv2.LINE_AA,
            )

            v1.cv2.putText(
                frame,
                line2,
                (x, y + 22),
                v1.cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                color,
                1,
                v1.cv2.LINE_AA,
            )

            v1.cv2.putText(
                frame,
                line3,
                (x, y + 42),
                v1.cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                color,
                1,
                v1.cv2.LINE_AA,
            )


# V1 main() resolves IncomingNode from its module globals.
# Replace only the node implementation; UI/main loop remains V1.
v1.IncomingNode = IncomingNodeV2


def main():
    return v1.main()


if __name__ == "__main__":
    raise SystemExit(main())
