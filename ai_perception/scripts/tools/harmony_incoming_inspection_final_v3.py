#!/usr/bin/env python3
"""Harmony Final Incoming Inspection V3.

Final V2 Server integration + Unity annotated video topic.

Important:
- Final V1 UI geometry remains unchanged.
- Final V2 Server transaction behavior remains unchanged.
- This layer only publishes the locked 1280x720 camera area
  from the final 1280x840 UI canvas.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sensor_msgs.msg import Image


ROOT = Path.home() / "vision_project"

BASE_RUNTIME = (
    ROOT
    / "scripts/tools/"
      "harmony_incoming_inspection_final_v2.py"
)

ANNOTATED_TOPIC = (
    "/vision/incoming_qa/annotated_image"
)


spec = importlib.util.spec_from_file_location(
    "harmony_incoming_inspection_final_v2_base",
    BASE_RUNTIME,
)

if spec is None or spec.loader is None:
    raise RuntimeError(
        f"cannot import V2 runtime: {BASE_RUNTIME}"
    )

v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)


class IncomingNodeV3(v2.IncomingNodeV2):
    def __init__(
        self,
        hb_ctx,
        contexts,
    ):
        super().__init__(
            hb_ctx,
            contexts,
        )

        self.annotated_pub = self.create_publisher(
            Image,
            ANNOTATED_TOPIC,
            1,
        )

        self.annotated_seq = 0

        print()
        print(
            "UNITY_VIDEO_FRAME_TAP=ENABLED"
        )
        print(
            f"ANNOTATED_TOPIC={ANNOTATED_TOPIC}"
        )
        print(
            "ANNOTATED_FRAME=1280x720_BGR8"
        )
        print(
            "UI_GEOMETRY=INHERITED_UNCHANGED"
        )
        print(
            "PRODUCTION_RUNTIME_AUTHORIZED=false"
        )

    def publish_annotated_frame(
        self,
        frame,
    ):
        if frame.shape[:2] != (
            720,
            1280,
        ):
            return

        msg = Image()

        msg.header.stamp = (
            self.get_clock()
            .now()
            .to_msg()
        )

        msg.header.frame_id = (
            "harmony_incoming_annotated"
        )

        msg.height = 720
        msg.width = 1280
        msg.encoding = "bgr8"
        msg.is_bigendian = 0
        msg.step = 1280 * 3
        msg.data = frame.tobytes()

        self.annotated_pub.publish(
            msg
        )

        self.annotated_seq += 1

    def render(
        self,
    ):
        canvas = super().render()

        # UI LOCK:
        # canvas = 1280x840
        # camera area = y 70..789
        # Do not resize or alter the canvas.
        annotated = (
            canvas[
                70:790,
                0:1280,
                :
            ]
            .copy()
        )

        self.publish_annotated_frame(
            annotated
        )

        return canvas


# V2 main eventually executes V1 main and resolves this symbol.
v2.v1.IncomingNode = IncomingNodeV3


def main():
    return v2.v1.main()


if __name__ == "__main__":
    raise SystemExit(main())
