#!/usr/bin/env python3
"""Harmony Vision -> Unity UDP JPEG Video Wire V2.

HMV1 wire:
  32-byte network-order header
  JPEG payload chunks <= 1200 bytes

Incoming:
  stream_id=1
  default topic=/vision/incoming_qa/annotated_image
  default port=21010
  target=10 FPS
  JPEG quality=80

PRE_ROOF:
  stream_id=2
  default topic=/vision/pre_roof/annotated_image
  default port=21020

No ACK/retransmission.
Incomplete frames are dropped by Unity receiver.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import time

import cv2
import numpy as np
import rclpy

from rclpy.node import Node
from sensor_msgs.msg import Image


MAGIC = b"HMV1"
VERSION = 1
HEADER_SIZE = 32
PAYLOAD_MAX = 1200

HEADER = struct.Struct(
    "!4sBBHIQHHIHH"
)

assert HEADER.size == HEADER_SIZE


STREAMS = {
    "incoming": {
        "stream_id": 1,
        "topic":
            "/vision/incoming_qa/annotated_image",
        "port": 21010,
        "width": 1280,
        "height": 720,
    },
    "pre_roof": {
        "stream_id": 2,
        "topic":
            "/vision/pre_roof/annotated_image",
        "port": 21020,
        "width": 1280,
        "height": 720,
    },
}


def image_to_bgr(
    msg: Image,
):
    if msg.encoding != "bgr8":
        raise ValueError(
            f"unsupported encoding={msg.encoding}"
        )

    if msg.step < msg.width * 3:
        raise ValueError(
            f"invalid step={msg.step}"
        )

    raw = np.frombuffer(
        msg.data,
        dtype=np.uint8,
    )

    expected = (
        msg.height
        * msg.step
    )

    if raw.size < expected:
        raise ValueError(
            "image buffer smaller than "
            f"height*step: {raw.size} < {expected}"
        )

    rows = raw[
        :expected
    ].reshape(
        msg.height,
        msg.step,
    )

    packed = rows[
        :,
        :msg.width * 3,
    ]

    return packed.reshape(
        msg.height,
        msg.width,
        3,
    ).copy()


class UnityVideoUdpSender(Node):
    def __init__(
        self,
        *,
        stream_name,
        host,
        port,
        topic,
        fps,
        quality,
    ):
        super().__init__(
            "harmony_unity_video_udp_v1"
        )

        cfg = STREAMS[
            stream_name
        ]

        self.stream_name = stream_name
        self.stream_id = int(
            cfg["stream_id"]
        )

        self.host = host
        self.port = int(port)
        self.topic = topic

        self.frame_period = (
            1.0 / float(fps)
        )

        self.quality = int(
            quality
        )

        self.expected_width = int(
            cfg["width"]
        )
        self.expected_height = int(
            cfg["height"]
        )

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.last_send = 0.0
        self.frame_id = 0
        self.sent_frames = 0
        self.dropped_frames = 0

        self.create_subscription(
            Image,
            self.topic,
            self.image_cb,
            1,
        )

        print(
            "============================================================"
        )
        print(
            "HARMONY UNITY VIDEO UDP V1"
        )
        print(
            "============================================================"
        )
        print(
            f"STREAM={self.stream_name}"
        )
        print(
            f"STREAM_ID={self.stream_id}"
        )
        print(
            f"TOPIC={self.topic}"
        )
        print(
            f"DEST={self.host}:{self.port}"
        )
        print(
            f"FRAME={self.expected_width}x"
            f"{self.expected_height}"
        )
        print(
            f"FPS={fps}"
        )
        print(
            f"JPEG_QUALITY={self.quality}"
        )
        print(
            f"PAYLOAD_MAX={PAYLOAD_MAX}"
        )
        print(
            f"DATAGRAM_MAX="
            f"{HEADER_SIZE + PAYLOAD_MAX}"
        )
        print(
            "WIRE=HMV1"
        )
        print(
            "ACK=false"
        )
        print(
            "RETRANSMIT=false"
        )

    def image_cb(
        self,
        msg,
    ):
        now = time.monotonic()

        if (
            now - self.last_send
            < self.frame_period
        ):
            return

        self.last_send = now

        try:
            frame = image_to_bgr(
                msg
            )
        except ValueError as exc:
            self.get_logger().warning(
                f"frame rejected: {exc}"
            )
            return

        if (
            frame.shape[1]
            != self.expected_width
            or frame.shape[0]
            != self.expected_height
        ):
            self.get_logger().warning(
                "frame size rejected: "
                f"{frame.shape[1]}x"
                f"{frame.shape[0]} "
                f"expected="
                f"{self.expected_width}x"
                f"{self.expected_height}"
            )
            return

        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [
                cv2.IMWRITE_JPEG_QUALITY,
                self.quality,
            ],
        )

        if not ok:
            self.dropped_frames += 1
            return

        jpeg = encoded.tobytes()

        frame_size = len(jpeg)

        chunk_count = (
            frame_size
            + PAYLOAD_MAX
            - 1
        ) // PAYLOAD_MAX

        if chunk_count > 65535:
            self.dropped_frames += 1
            return

        timestamp_ms = (
            time.time_ns()
            // 1_000_000
        )

        frame_id = (
            self.frame_id
            & 0xFFFFFFFF
        )

        for chunk_index in range(
            chunk_count
        ):
            start = (
                chunk_index
                * PAYLOAD_MAX
            )

            payload = jpeg[
                start:
                start + PAYLOAD_MAX
            ]

            header = HEADER.pack(
                MAGIC,
                VERSION,
                self.stream_id,
                HEADER_SIZE,
                frame_id,
                timestamp_ms,
                chunk_index,
                chunk_count,
                frame_size,
                len(payload),
                0,
            )

            self.sock.sendto(
                header + payload,
                (
                    self.host,
                    self.port,
                ),
            )

        self.frame_id = (
            self.frame_id + 1
        ) & 0xFFFFFFFF

        self.sent_frames += 1

        if (
            self.sent_frames % 100
            == 0
        ):
            print(
                "VIDEO_TX "
                f"frames={self.sent_frames} "
                f"last_frame_id={frame_id} "
                f"jpeg_bytes={frame_size} "
                f"chunks={chunk_count}",
                flush=True,
            )

    def destroy_node(
        self,
    ):
        try:
            self.sock.close()
        finally:
            super().destroy_node()


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--stream",
        choices=sorted(
            STREAMS
        ),
        default="incoming",
    )

    parser.add_argument(
        "--host",
        default=os.environ.get(
            "UNITY_VIDEO_HOST"
        ),
    )

    parser.add_argument(
        "--port",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--topic",
        default=None,
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
    )

    parser.add_argument(
        "--quality",
        type=int,
        default=80,
    )

    args = parser.parse_args()

    if not args.host:
        parser.error(
            "--host or UNITY_VIDEO_HOST "
            "is required"
        )

    if not (
        1 <= args.quality <= 100
    ):
        parser.error(
            "--quality must be 1..100"
        )

    if args.fps <= 0:
        parser.error(
            "--fps must be > 0"
        )

    cfg = STREAMS[
        args.stream
    ]

    if args.port is None:
        args.port = int(
            cfg["port"]
        )

    if args.topic is None:
        args.topic = str(
            cfg["topic"]
        )

    if not (
        1 <= args.port <= 65535
    ):
        parser.error(
            "--port must be 1..65535"
        )

    return args


def main():
    args = parse_args()

    rclpy.init()

    node = UnityVideoUdpSender(
        stream_name=args.stream,
        host=args.host,
        port=args.port,
        topic=args.topic,
        fps=args.fps,
        quality=args.quality,
    )

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
