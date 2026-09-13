#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V7 UI.

Operator-triggered server transaction layer over Final V6.

V7 POLICY:
- Server request is queued only.
- Server request never starts inspection automatically.
- Server request never changes UI mode automatically.
- Operator keys 1/2/3 select mode.
- SPACE starts inspection.
- If a queued server request matches the current mode,
  SPACE binds that request and the result is transmitted to server.
- If no matching server request exists,
  SPACE remains a manual inspection with no server transmit.
- RESULT stays displayed until operator action / next inspection.

Inherited unchanged from V6:
- HOUSE_B RAW preview fallback
- HOUSE_B actual inspection requires fresh aligned frame
- AI / ROI / thresholds
- UDP result contract
- Unity annotated video
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import rclpy

from std_msgs.msg import String


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT /
    "scripts/tools/"
    "harmony_incoming_inspection_final_v6_ui.py"
)

spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v6_base",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import V6 runtime: {BASE_RUNTIME}"
    )

v6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v6)


class IncomingNodeV7(v6.IncomingNodeV6):

    def server_request_cb(self, msg):
        try:
            payload = json.loads(msg.data)
        except Exception:
            # Canonical malformed-request handling remains inherited.
            return v6.BaseIncomingNode.server_request_cb(
                self,
                msg,
            )

        mode = payload.get("inspection_mode")
        request_id = str(
            payload.get("inspection_request_id", "")
        )

        # Unknown mode -> inherited validation/rejection.
        if mode not in self.contexts:
            return v6.BaseIncomingNode.server_request_cb(
                self,
                msg,
            )

        if (
            request_id
            and request_id in self.pending_request_ids
        ):
            print(
                f"SERVER_REQUEST_PENDING_DUPLICATE={request_id}",
                flush=True,
            )
            return

        # HOUSE_B recovery guard only.
        # Ignore a recovery re-publish when the same HOUSE_B
        # request is already the active server transaction.
        active_request = getattr(
            self,
            "active_server_request",
            None,
        )

        if (
            mode == "HOUSE_B"
            and request_id
            and isinstance(active_request, dict)
            and str(
                active_request.get(
                    "inspection_request_id",
                    "",
                )
            ) == request_id
        ):
            print(
                "HOUSE_B_SERVER_REQUEST_ACTIVE_DUPLICATE="
                + request_id,
                flush=True,
            )
            return

        # Never auto-start.
        # Every valid server request waits for operator SPACE.
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
        print("SERVER_REQUEST_QUEUED=WAIT_FOR_SPACE")
        print(f"REQUEST_ID={request_id}")
        print(f"REQUEST_MODE={mode}")
        print(f"CURRENT_UI_MODE={self.mode_name}")
        print("AUTO_INSPECTION=false")
        print("AUTO_MODE_CHANGE=false")
        print("RESULT_SCREEN_PRESERVED=true")
        print("ACTIVATE_BY=SPACE")
        print(
            "============================================================"
        )
        print(flush=True)

    def set_mode(self, mode_name):
        # Bypass V6's automatic pending-request activation.
        # Keep V5/V2 operator mode-control behavior.
        return v6.BaseIncomingNode.set_mode(
            self,
            mode_name,
        )

    def _pop_matching_pending_request(self):
        for i, raw in enumerate(
            self.pending_server_requests
        ):
            try:
                payload = json.loads(raw)
            except Exception:
                continue

            if (
                payload.get("inspection_mode")
                == self.mode_name
            ):
                raw = self.pending_server_requests.pop(i)

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

                return raw

        return None

    def start_inspection(self):
        # A previous server transaction may be completed and
        # its RESULT screen still latched.
        if (
            self.active_server_request is not None
            and self.state == "RESULT"
        ):
            self.clear_completed_transaction()

        # An active non-completed server transaction is protected.
        if self.transaction_locked():
            print(
                "SPACE_BLOCKED="
                "SERVER_TRANSACTION_ACTIVE"
            )
            return

        raw = self._pop_matching_pending_request()

        # HOUSE_B server-bind race guard only.
        # Keep operator flow unchanged: key 2 -> SPACE.
        # If the Gateway has just published the server request,
        # allow a short ROS2 callback window before falling back
        # to the existing manual inspection path.
        if (
            raw is None
            and self.mode_name == "HOUSE_B"
        ):
            print(
                "HOUSE_B_SPACE_WAITING_FOR_SERVER_REQUEST="
                "0.50s",
                flush=True,
            )

            deadline = time.monotonic() + 0.50

            while (
                raw is None
                and time.monotonic() < deadline
                and rclpy.ok()
            ):
                rclpy.spin_once(
                    self,
                    timeout_sec=0.02,
                )

                raw = (
                    self._pop_matching_pending_request()
                )

            if raw is not None:
                print(
                    "HOUSE_B_SERVER_REQUEST_BOUND_AFTER_WAIT",
                    flush=True,
                )

        if raw is None:
            print()
            print(
                "SPACE_INSPECTION="
                "MANUAL_NO_SERVER_REQUEST"
            )

            # Existing manual inspection behavior.
            return v6.BaseIncomingNode.start_inspection(
                self
            )

        payload = json.loads(raw)

        print()
        print(
            "============================================================"
        )
        print(
            "SPACE_SERVER_REQUEST_ACTIVATED"
        )
        print(
            "INSPECTION_REQUEST_ID="
            + str(
                payload["inspection_request_id"]
            )
        )
        print(
            "MODE="
            + str(
                payload["inspection_mode"]
            )
        )
        print(
            "RESULT_WILL_TRANSMIT_TO_SERVER=true"
        )
        print(
            "============================================================"
        )

        msg = String()
        msg.data = raw

        # Mode matches current UI mode.
        # V5/V2 performs the original full validation,
        # creates active_server_request and starts INSPECTING.
        return v6.BaseIncomingNode.server_request_cb(
            self,
            msg,
        )


# Node ultimately instantiated by V1 main.
v6.v5.v4.v3.v2.v1.IncomingNode = IncomingNodeV7


print()
print(
    "============================================================"
)
print(
    "HARMONY FINAL INCOMING INSPECTION V7 UI"
)
print(
    "============================================================"
)
print(
    "SERVER_REQUEST_POLICY=QUEUE_UNTIL_SPACE"
)
print(
    "MODE_OWNER=OPERATOR_KEYS_1_2_3"
)
print(
    "INSPECTION_TRIGGER=SPACE"
)
print(
    "SERVER_RESULT_ON_MATCHED_REQUEST=ENABLED"
)
print(
    "RESULT_SCREEN_LATCH=ENABLED"
)
print(
    "HOUSE_B_RAW_PREVIEW=V6_INHERITED"
)
print(
    "HOUSE_B_INSPECTION=ALIGNED_ONLY"
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
    return v6.main()


if __name__ == "__main__":
    raise SystemExit(main())
