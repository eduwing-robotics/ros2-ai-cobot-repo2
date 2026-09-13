#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V5 UI.

Control-policy layer over verified Final Incoming Runtime V4.

V5 CHANGE ONLY:
- UI mode is controlled only by operator keys 1/2/3.
- Server request cannot force a mode change.
- Server request is accepted only when its inspection_mode matches
  the currently selected UI mode.
- PASS/FAIL RESULT remains displayed until:
    * another request for the same mode starts, or
    * the operator manually changes mode.
- Existing AI / ROI / thresholds / server result / Unity video /
  UI geometry / palette remain unchanged.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT
    / "scripts/tools/"
      "harmony_incoming_inspection_final_v4_ui.py"
)


spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v4_base",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import V4 runtime: {BASE_RUNTIME}"
    )

v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)


BaseIncomingNode = v4.v3.IncomingNodeV3


class IncomingNodeV5(BaseIncomingNode):

    def server_request_cb(
        self,
        msg,
    ):
        # First inspect only the requested mode.
        # Full contract validation remains in the inherited V2 handler.
        try:
            payload = json.loads(
                msg.data
            )
        except Exception:
            # Let the inherited handler produce the canonical BAD_JSON log.
            return super().server_request_cb(
                msg
            )

        requested_mode = payload.get(
            "inspection_mode"
        )

        # Only valid known modes participate in this control gate.
        # Unknown/bad modes are delegated to the inherited validator.
        if (
            requested_mode in self.contexts
            and requested_mode != self.mode_name
        ):
            print()
            print(
                "============================================================"
            )
            print(
                "SERVER_REQUEST_REJECTED=MODE_MISMATCH"
            )
            print(
                f"UI_MODE={self.mode_name}"
            )
            print(
                f"REQUEST_MODE={requested_mode}"
            )
            print(
                "MODE_CHANGE_REQUIRED="
                "OPERATOR_KEY_1_2_3"
            )
            print(
                "CURRENT_RESULT_SCREEN=PRESERVED"
            )
            print(
                "============================================================"
            )
            print(
                flush=True
            )
            return

        # Same-mode request:
        # inherited V2 behavior is preserved.
        #
        # If previous state == RESULT:
        #   completed transaction is released,
        #   histories reset,
        #   new inspection starts.
        #
        # self.mode_name = requested_mode inside V2 is harmless here
        # because requested_mode == current UI mode.
        return super().server_request_cb(
            msg
        )


# V4 -> V3 -> V2 -> V1 main eventually resolves this symbol.
v4.v3.v2.v1.IncomingNode = IncomingNodeV5


print()
print(
    "============================================================"
)
print(
    "HARMONY FINAL INCOMING INSPECTION V5 UI"
)
print(
    "============================================================"
)
print(
    "BASE_RUNTIME=FINAL_V4_UNCHANGED"
)
print(
    "MODE_OWNER=OPERATOR_KEYS_1_2_3"
)
print(
    "SERVER_FORCE_MODE_CHANGE=DISABLED"
)
print(
    "SERVER_SAME_MODE_REQUEST=ENABLED"
)
print(
    "RESULT_SCREEN_LATCH=ENABLED"
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
    "SERVER_RESULT_CHANGE=0"
)
print(
    "UNITY_CHANGE=0"
)
print(
    "UI_GEOMETRY_CHANGE=0"
)
print(
    "PRODUCTION_RUNTIME_AUTHORIZED=false"
)
print(
    "============================================================"
)
print()


def main():
    return v4.main()


if __name__ == "__main__":
    raise SystemExit(main())
