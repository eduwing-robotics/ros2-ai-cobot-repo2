#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSProfile,
    ReliabilityPolicy,
    HistoryPolicy,
)
from sensor_msgs.msg import Image


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = (
    ROOT
    / "config"
    / "factory_view"
    / "factory_view_final_v1.json"
)

TOPIC = "/vision/factory_camera/image_view"
FRAME_ID = "harmony_factory_camera"


def read_exact(pipe, size: int) -> bytes:
    chunks = []
    remaining = size

    while remaining > 0:
        chunk = pipe.read(remaining)

        if not chunk:
            break

        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


class FactoryCameraPublisher(Node):
    def __init__(self):
        super().__init__(
            "harmony_factory_camera_publisher_v1"
        )

        cfg = json.loads(
            CONFIG_PATH.read_text()
        )

        if cfg.get("status") != "LOCKED_FINAL":
            raise RuntimeError(
                "Factory View config is not LOCKED_FINAL"
            )

        if cfg["policy"]["retuning_allowed"] is not False:
            raise RuntimeError(
                "Factory View retuning policy mismatch"
            )

        self.device = str(cfg["device"])
        self.width = int(
            cfg["output"]["width"]
        )
        self.height = int(
            cfg["output"]["height"]
        )
        self.fps = int(
            cfg["capture"]["fps"]
        )
        self.video_filter = str(
            cfg["video_filter"]
        )

        controls = cfg["camera_controls"]

        self.get_logger().info(
            "Applying LOCKED Factory View controls"
        )

        ctrl_query = subprocess.run(
            [
                "v4l2-ctl",
                "-d",
                self.device,
                "--list-ctrls",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

        supported_controls = set()

        for line in ctrl_query.stdout.splitlines():
            line = line.strip()

            if not line:
                continue

            name = line.split()[0]

            if "0x" in line:
                supported_controls.add(name)

        for key, value in controls.items():

            if key not in supported_controls:
                self.get_logger().warning(
                    f"Skipping unsupported V4L2 control: "
                    f"{key}={value}"
                )
                continue

            subprocess.run(
                [
                    "v4l2-ctl",
                    "-d",
                    self.device,
                    f"--set-ctrl={key}={value}",
                ],
                check=True,
            )

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )

        self.publisher = self.create_publisher(
            Image,
            TOPIC,
            qos,
        )

        self.frame_bytes = (
            self.width
            * self.height
            * 3
        )

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "v4l2",
            "-input_format",
            "mjpeg",
            "-video_size",
            "1280x960",
            "-framerate",
            str(self.fps),
            "-i",
            self.device,
            "-vf",
            self.video_filter,
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]

        self.get_logger().info(
            "Starting LOCKED Factory View FFmpeg pipeline"
        )

        self.proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            bufsize=self.frame_bytes * 2,
        )

        if self.proc.stdout is None:
            raise RuntimeError(
                "FFmpeg stdout pipe unavailable"
            )

        self.get_logger().info(
            f"TOPIC={TOPIC}"
        )
        self.get_logger().info(
            f"SIZE={self.width}x{self.height}"
        )
        self.get_logger().info(
            "ENCODING=bgr8"
        )
        self.get_logger().info(
            f"FPS={self.fps}"
        )
        self.get_logger().info(
            "FACTORY_VIEW_CONFIG=LOCKED_FINAL"
        )

    def run(self):
        frame_count = 0

        while rclpy.ok():
            raw = read_exact(
                self.proc.stdout,
                self.frame_bytes,
            )

            if len(raw) != self.frame_bytes:
                self.get_logger().error(
                    "FFmpeg frame ended unexpectedly"
                )
                break

            msg = Image()
            msg.header.stamp = (
                self.get_clock()
                .now()
                .to_msg()
            )
            msg.header.frame_id = FRAME_ID

            msg.height = self.height
            msg.width = self.width
            msg.encoding = "bgr8"
            msg.is_bigendian = 0
            msg.step = self.width * 3
            msg.data = raw

            self.publisher.publish(msg)

            frame_count += 1

            if frame_count % (
                self.fps * 5
            ) == 0:
                self.get_logger().info(
                    f"PUBLISHED_FRAMES={frame_count}"
                )

    def destroy_node(self):
        proc = getattr(
            self,
            "proc",
            None,
        )

        if proc is not None:
            if proc.poll() is None:
                proc.terminate()

                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()

        super().destroy_node()


def main():
    rclpy.init()

    node = FactoryCameraPublisher()

    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
