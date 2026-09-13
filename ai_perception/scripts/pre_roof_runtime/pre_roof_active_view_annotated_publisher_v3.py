#!/usr/bin/env python3

from __future__ import annotations

import json
import threading
import time
import urllib.request

import cv2
import numpy as np
import rclpy

from rclpy.node import Node
from sensor_msgs.msg import Image


DASHBOARD_STATE = (
    "http://127.0.0.1:8811/api/state"
)

D435_RAW_URL = (
    "http://127.0.0.1:8820/frame.jpg"
)

TOPIC = (
    "/vision/pre_roof/annotated_image"
)

WIDTH = 1280
HEIGHT = 720


class ViewChanged(Exception):
    pass


def get_dashboard_state():

    with urllib.request.urlopen(
        DASHBOARD_STATE,
        timeout=2.0,
    ) as r:

        return json.loads(
            r.read().decode(
                "utf-8"
            )
        )


def get_d435_raw():

    with urllib.request.urlopen(
        D435_RAW_URL,
        timeout=2.0,
    ) as r:

        data = r.read()

    frame = cv2.imdecode(
        np.frombuffer(
            data,
            dtype=np.uint8,
        ),
        cv2.IMREAD_COLOR,
    )

    if frame is None:
        raise RuntimeError(
            "D435 RAW decode failed"
        )

    return frame


class ActiveQcPublisher(Node):

    def __init__(self):

        super().__init__(
            "harmony_pre_roof_active_qc_publisher_v3"
        )

        self.pub = self.create_publisher(
            Image,
            TOPIC,
            1,
        )

        self.running = True

        self.worker = threading.Thread(
            target=self.loop,
            daemon=True,
        )

        self.worker.start()

        print(
            "============================================================",
            flush=True,
        )

        print(
            "HARMONY PRE_ROOF ACTIVE QC VIDEO PUBLISHER V3",
            flush=True,
        )

        print(
            f"TOPIC      : {TOPIC}",
            flush=True,
        )

        print(
            "SOURCE     : DASHBOARD ACTIVE VIEW",
            flush=True,
        )

        print(
            "FRAME      : 1280x720 BGR8",
            flush=True,
        )

        print(
            "AUTO VIEW  : TOP/LEFT/RIGHT/FRONT/BEHIND",
            flush=True,
        )

        print(
            "============================================================",
            flush=True,
        )


    def publish_frame(
        self,
        frame,
    ):

        if frame is None:
            return

        if frame.shape[:2] != (
            HEIGHT,
            WIDTH,
        ):

            frame = cv2.resize(
                frame,
                (
                    WIDTH,
                    HEIGHT,
                ),
                interpolation=cv2.INTER_AREA,
            )

        if not frame.flags[
            "C_CONTIGUOUS"
        ]:
            frame = np.ascontiguousarray(
                frame
            )

        msg = Image()

        msg.header.stamp = (
            self.get_clock()
            .now()
            .to_msg()
        )

        msg.header.frame_id = (
            "harmony_pre_roof_active_qc"
        )

        msg.height = HEIGHT
        msg.width = WIDTH
        msg.encoding = "bgr8"
        msg.is_bigendian = 0
        msg.step = WIDTH * 3
        msg.data = frame.tobytes()

        self.pub.publish(
            msg
        )


    def waiting_frame(
        self,
    ):

        frame = np.zeros(
            (
                HEIGHT,
                WIDTH,
                3,
            ),
            dtype=np.uint8,
        )

        cv2.putText(
            frame,
            "HARMONY PRE-ROOF QUALITY INSPECTION",
            (70, 300),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.15,
            (230, 230, 230),
            2,
            cv2.LINE_AA,
        )

        cv2.putText(
            frame,
            "WAITING FOR SERVER VIEW REQUEST",
            (70, 370),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (170, 170, 170),
            2,
            cv2.LINE_AA,
        )

        return frame


    def stream_view(
        self,
        view,
        port,
    ):

        url = (
            f"http://127.0.0.1:"
            f"{port}/stream"
        )

        print(
            f"[VIEW] {view} "
            f"-> {url}",
            flush=True,
        )

        with urllib.request.urlopen(
            url,
            timeout=5.0,
        ) as r:

            buf = b""
            last_check = 0.0

            while (
                self.running
                and
                rclpy.ok()
            ):

                chunk = r.read(
                    4096
                )

                if not chunk:
                    return

                buf += chunk

                while True:

                    start = buf.find(
                        b"\xff\xd8"
                    )

                    if start < 0:
                        break

                    end = buf.find(
                        b"\xff\xd9",
                        start + 2,
                    )

                    if end < 0:
                        break

                    jpg = buf[
                        start:end + 2
                    ]

                    buf = buf[
                        end + 2:
                    ]

                    frame = cv2.imdecode(
                        np.frombuffer(
                            jpg,
                            dtype=np.uint8,
                        ),
                        cv2.IMREAD_COLOR,
                    )

                    if frame is not None:

                        self.publish_frame(
                            frame
                        )

                    now = time.time()

                    if (
                        now
                        - last_check
                        >= 0.25
                    ):

                        last_check = now

                        state = (
                            get_dashboard_state()
                        )

                        if (
                            state.get(
                                "current_view"
                            )
                            != view
                        ):
                            raise ViewChanged()


    def loop(
        self,
    ):

        last_wait = 0.0

        while (
            self.running
            and
            rclpy.ok()
        ):

            try:

                state = (
                    get_dashboard_state()
                )

                view = state.get(
                    "current_view"
                )

                if not view:

                    try:

                        frame = (
                            get_d435_raw()
                        )

                        self.publish_frame(
                            frame
                        )

                    except Exception as e:

                        print(
                            f"[RAW WARN] {e}",
                            flush=True,
                        )

                        self.publish_frame(
                            self.waiting_frame()
                        )

                    time.sleep(
                        0.05
                    )

                    continue

                profiles = (
                    state.get(
                        "profiles",
                        {}
                    )
                )

                profile = profiles.get(
                    view,
                    {}
                )

                port = profile.get(
                    "runtime_port"
                )

                if not port:

                    time.sleep(
                        0.2
                    )

                    continue

                try:

                    self.stream_view(
                        view,
                        int(port),
                    )

                except ViewChanged:

                    print(
                        f"[VIEW CHANGE] "
                        f"{view}",
                        flush=True,
                    )

                    continue

            except Exception as e:

                print(
                    f"[WARN] {e}",
                    flush=True,
                )

                time.sleep(
                    0.5
                )


    def destroy_node(
        self,
    ):

        self.running = False

        super().destroy_node()


def main():

    rclpy.init()

    node = ActiveQcPublisher()

    try:

        rclpy.spin(
            node
        )

    except KeyboardInterrupt:

        pass

    finally:

        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
