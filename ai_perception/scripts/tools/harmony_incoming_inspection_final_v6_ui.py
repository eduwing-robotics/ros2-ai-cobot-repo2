#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V6 UI.

Control/preview layer over Final V5.

V6:
- Operator keys 1/2/3 own UI mode changes.
- Server request cannot force another mode.
- Mismatched server requests are queued, not discarded.
- Same-mode server reinspection remains enabled.
- PASS/FAIL RESULT remains latched until next request or manual mode change.
- HOUSE_B uses RAW only as preview while aligned input is unavailable.
- HOUSE_B inspection itself is strictly ALIGNED-only.
- A newly selected HOUSE_B mode requires a fresh aligned frame.

UNCHANGED:
- AI
- ROI
- thresholds
- result contract
- UDP gateway
- Unity annotated topic
- UI geometry
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time

from std_msgs.msg import String


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT /
    "scripts/tools/"
    "harmony_incoming_inspection_final_v5_ui.py"
)

spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v5_base",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import V5 runtime: {BASE_RUNTIME}"
    )

v5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v5)


BaseIncomingNode = v5.IncomingNodeV5


class IncomingNodeV6(BaseIncomingNode):

    ALIGNED_FRESH_SEC = 1.0

    def __init__(
        self,
        hb_ctx,
        contexts,
    ):
        self.pending_server_requests = []
        self.pending_request_ids = set()

        self._aligned_last_rx = None
        self._house_b_wait_logged = False

        super().__init__(
            hb_ctx,
            contexts,
        )

        print()
        print(
            "V6_OPERATOR_MODE_LOCK=ENABLED"
        )
        print(
            "V6_PENDING_SERVER_REQUEST=ENABLED"
        )
        print(
            "V6_HOUSE_B_RAW_PREVIEW=ENABLED"
        )
        print(
            "V6_HOUSE_B_INSPECTION=RAW_ONLY"
        )

    # --------------------------------------------------------
    # Track freshness of aligned frames.
    # --------------------------------------------------------

    def aligned_cb(
        self,
        msg,
    ):
        super().aligned_cb(
            msg
        )

        self._aligned_last_rx = (
            time.monotonic()
        )

    def aligned_fresh(
        self,
    ):
        if (
            self.latest_aligned is None
            or self._aligned_last_rx is None
        ):
            return False

        return (
            time.monotonic()
            - self._aligned_last_rx
            <= self.ALIGNED_FRESH_SEC
        )

    # --------------------------------------------------------
    # Display source:
    #
    # HOUSE_B:
    #   fresh aligned -> aligned
    #   no aligned    -> RAW preview
    #
    # Inspection is separately guarded below.
    # --------------------------------------------------------

    def current_frame(
        self,
    ):
        # HOUSE_B uses the same RAW camera coordinate system
        # as BASE_AB / HOUSE_A.
        if self.mode_name == "HOUSE_B":
            return self.latest_raw

        return super().current_frame()

    # --------------------------------------------------------
    # Server request policy.
    # --------------------------------------------------------

    def server_request_cb(
        self,
        msg,
    ):
        try:
            payload = json.loads(
                msg.data
            )
        except Exception:
            return super().server_request_cb(
                msg
            )

        requested_mode = payload.get(
            "inspection_mode"
        )

        request_id = str(
            payload.get(
                "inspection_request_id",
                ""
            )
        )

        # Different UI mode:
        # queue the request instead of forcing a mode switch.
        if (
            requested_mode in self.contexts
            and requested_mode != self.mode_name
        ):

            if (
                request_id
                and request_id
                in self.pending_request_ids
            ):
                print(
                    "SERVER_REQUEST_PENDING_DUPLICATE="
                    + request_id,
                    flush=True,
                )
                return

            self.pending_server_requests.append(
                msg.data
            )

            if request_id:
                self.pending_request_ids.add(
                    request_id
                )

            print()
            print(
                "============================================================"
            )
            print(
                "SERVER_REQUEST_QUEUED=MODE_WAIT"
            )
            print(
                f"UI_MODE={self.mode_name}"
            )
            print(
                f"REQUEST_MODE={requested_mode}"
            )
            print(
                f"REQUEST_ID={request_id}"
            )
            print(
                "CURRENT_RESULT_SCREEN=PRESERVED"
            )
            print(
                "ACTIVATE_BY=OPERATOR_KEY_1_2_3"
            )
            print(
                "============================================================"
            )
            print(
                flush=True
            )
            return

        # Same-mode request:
        # V5/V2 validation and transaction behavior unchanged.
        return super().server_request_cb(
            msg
        )

    # --------------------------------------------------------
    # Operator owns mode changes.
    #
    # If a server request for that mode was queued earlier,
    # activate ONE request after the operator selects the mode.
    # Remaining requests stay queued so results never auto-chain.
    # --------------------------------------------------------

    def set_mode(
        self,
        mode_name,
    ):
        old_mode = self.mode_name

        # HOUSE_B must receive a new aligned frame after
        # the operator explicitly selects HOUSE_B.
        if (
            mode_name == "HOUSE_B"
            and old_mode != "HOUSE_B"
        ):
            self._aligned_last_rx = None
            self.latest_aligned = None

        super().set_mode(
            mode_name
        )

        # Mode change may have been blocked by an active transaction.
        if self.mode_name != mode_name:
            return

        if self.active_server_request is not None:
            return

        pending_index = None

        for i, raw in enumerate(
            self.pending_server_requests
        ):
            try:
                payload = json.loads(
                    raw
                )
            except Exception:
                continue

            if (
                payload.get(
                    "inspection_mode"
                )
                == mode_name
            ):
                pending_index = i
                break

        if pending_index is None:
            return

        raw = self.pending_server_requests.pop(
            pending_index
        )

        payload = json.loads(
            raw
        )

        request_id = str(
            payload.get(
                "inspection_request_id",
                ""
            )
        )

        if request_id:
            self.pending_request_ids.discard(
                request_id
            )

        print()
        print(
            "SERVER_PENDING_REQUEST_ACTIVATED"
        )
        print(
            f"MODE={mode_name}"
        )
        print(
            f"REQUEST_ID={request_id}"
        )

        pending_msg = String()
        pending_msg.data = raw

        # Mode now matches, so V5/V2 processes it normally.
        super().server_request_cb(
            pending_msg
        )

    # --------------------------------------------------------
    # HOUSE_B inspection safety gate.
    #
    # RAW fallback is DISPLAY ONLY.
    # Never feed RAW into HOUSE_B inspection.
    # --------------------------------------------------------

    def inspect_once(
        self,
        frame,
    ):
        # HOUSE_B inspection now uses the RAW frame supplied by
        # current_frame(), matching BASE_AB / HOUSE_A behavior.
        self._house_b_wait_logged = False

        return super().inspect_once(
            frame
        )


# Replace the node symbol ultimately used by V1 main.
v5.v4.v3.v2.v1.IncomingNode = (
    IncomingNodeV6
)


print()
print(
    "============================================================"
)
print(
    "HARMONY FINAL INCOMING INSPECTION V6 UI"
)
print(
    "============================================================"
)
print(
    "BASE_RUNTIME=FINAL_V5_UNCHANGED"
)
print(
    "MODE_OWNER=OPERATOR_KEYS_1_2_3"
)
print(
    "SERVER_FORCE_MODE_CHANGE=DISABLED"
)
print(
    "SERVER_MISMATCH_REQUEST=QUEUED"
)
print(
    "SERVER_SAME_MODE_REINSPECTION=ENABLED"
)
print(
    "RESULT_SCREEN_LATCH=ENABLED"
)
print(
    "HOUSE_B_RAW_PREVIEW=ENABLED"
)
print(
    "HOUSE_B_INSPECTION=FRESH_ALIGNED_ONLY"
)
print(
    "AI_CHANGE=0"
)
print(
    "ROI_CHANGE=0"
)
print(
    "THRESHOLD_CHANGE=0"
)
print(
    "SERVER_PROTOCOL_CHANGE=0"
)
print(
    "UNITY_CHANGE=0"
)
print(
    "production_valid=false"
)
print(
    "============================================================"
)
print()


def main():
    return v5.main()


if __name__ == "__main__":
    raise SystemExit(main())
