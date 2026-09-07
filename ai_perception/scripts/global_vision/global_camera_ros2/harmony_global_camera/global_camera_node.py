import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote

import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import Image
from std_msgs.msg import String


class GlobalCameraNode(Node):
    def __init__(self):
        super().__init__('global_camera_node')

        self.declare_parameter('host', '192.168.20.28')
        self.declare_parameter('port', 8554)
        self.declare_parameter('username', 'oldbird')
        self.declare_parameter('width', 1280)
        self.declare_parameter('height', 720)
        self.declare_parameter('publish_fps', 30.0)
        self.declare_parameter('frame_id', 'global_camera')
        self.declare_parameter('stale_timeout_sec', 0.5)
        self.declare_parameter('reconnect_interval_sec', 2.0)

        self.host = self.get_parameter('host').value
        self.port = self.get_parameter('port').value
        self.username = self.get_parameter('username').value
        self.width = self.get_parameter('width').value
        self.height = self.get_parameter('height').value
        self.publish_fps = self.get_parameter('publish_fps').value
        self.frame_id = self.get_parameter('frame_id').value
        self.stale_timeout_sec = self.get_parameter(
            'stale_timeout_sec'
        ).value
        self.reconnect_interval_sec = self.get_parameter(
            'reconnect_interval_sec'
        ).value

        password = os.environ.get('OLD_BIRD_PASSWORD')
        if not password:
            raise RuntimeError(
                'OLD_BIRD_PASSWORD environment variable is not set'
            )

        password = quote(password, safe='')
        self.rtsp_url = (
            f'rtsp://{self.username}:{password}@'
            f'{self.host}:{self.port}/'
        )

        self.frame_bytes = self.width * self.height * 3

        self.running = True
        self.frame_count = 0
        self.published_count = 0
        self.latest_sequence = 0
        self.last_published_sequence = 0
        self.latest_frame = None
        self.last_frame_time = None
        self.start_time = time.monotonic()
        self.stream_stale = True
        self.reconnect_count = 0

        self.frame_lock = threading.Lock()
        self.stop_event = threading.Event()

        self.bridge = CvBridge()

        image_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
        )

        self.image_publisher = self.create_publisher(
            Image,
            '/vision/global_camera/image_raw',
            image_qos,
        )

        status_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )

        self.status_publisher = self.create_publisher(
            String,
            "/vision/global_camera/status",
            status_qos,
        )

        self.ffmpeg_process = None
        self.capture_thread = None

        self._start_ffmpeg()

        self.capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
        )
        self.capture_thread.start()

        self.publish_timer = self.create_timer(
            1.0 / self.publish_fps,
            self._publish_latest_frame,
        )

        self.stale_watchdog_timer = self.create_timer(
            0.1,
            self._check_stale_state,
        )

        self.status_timer = self.create_timer(
            5.0,
            self._print_status,
        )
        self.status_publish_timer = self.create_timer(
            1.0,
            self._publish_status,
        )


        self.get_logger().info(
            'Global camera receiver started '
            f'({self.width}x{self.height}, RTP/UDP low-latency)'
        )
        self.get_logger().info(
            'Publishing: /vision/global_camera/image_raw '
            '(BGR8, QoS BEST_EFFORT depth=1)'
        )

    def _start_ffmpeg(self):
        command = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'warning',
            '-nostdin',
            '-rtsp_transport', 'udp',
            '-fflags', 'nobuffer+discardcorrupt',
            '-flags', 'low_delay',
            '-reorder_queue_size', '0',
            '-max_delay', '0',
            '-analyzeduration', '1000000',
            '-probesize', '1000000',
            '-i', self.rtsp_url,
            '-map', '0:v:0',
            '-an',
            '-vf', 'transpose=cclock',
            '-fps_mode', 'passthrough',
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            'pipe:1',
        ]

        self.ffmpeg_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        threading.Thread(
            target=self._ffmpeg_stderr_loop,
            args=(self.ffmpeg_process,),
            daemon=True,
        ).start()

    @staticmethod
    def _sanitize_ffmpeg_log(message):
        return re.sub(
            r"rtsp://[^@\s]+@",
            "rtsp://***@",
            message,
        )

    def _ffmpeg_stderr_loop(self, process):
        if process.stderr is None:
            return

        while self.running:
            raw_line = process.stderr.readline()
            if not raw_line:
                break

            message = raw_line.decode(
                "utf-8",
                errors="replace",
            ).rstrip()

            if not message:
                continue

            message = self._sanitize_ffmpeg_log(message)
            if not self.running or not rclpy.ok():
                break

            self.get_logger().warning(
                f"FFmpeg: {message}"
            )

    def _stop_ffmpeg(self):
        process = self.ffmpeg_process

        self.ffmpeg_process = None

        if process is None:
            return

        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        if process.stdout is not None:
            process.stdout.close()


    @staticmethod
    def _read_exact(stream, size):
        chunks = []
        remaining = size

        while remaining > 0:
            chunk = stream.read(remaining)

            if not chunk:
                return None

            chunks.append(chunk)
            remaining -= len(chunk)

        return b''.join(chunks)

    def _capture_loop(self):
        first_frame_reported = False

        while self.running:
            if (
                self.ffmpeg_process is None
                or self.ffmpeg_process.stdout is None
            ):
                if self.stop_event.wait(
                    self.reconnect_interval_sec
                ):
                    break

                if not self.running:
                    break

                self.reconnect_count += 1
                self.get_logger().warning(
                    'RTSP reconnect attempt '
                    f'#{self.reconnect_count}'
                )
                self._start_ffmpeg()
                continue

            data = self._read_exact(
                self.ffmpeg_process.stdout,
                self.frame_bytes,
            )

            if data is None:
                if not self.running:
                    break

                self.get_logger().error(
                    'FFmpeg frame stream ended'
                )

                self._stop_ffmpeg()

                if self.stop_event.wait(
                    self.reconnect_interval_sec
                ):
                    break

                if not self.running:
                    break

                self.reconnect_count += 1
                self.get_logger().warning(
                    'RTSP reconnect attempt '
                    f'#{self.reconnect_count}'
                )
                self._start_ffmpeg()
                continue

            frame = np.frombuffer(
                data,
                dtype=np.uint8,
            ).reshape(
                (self.height, self.width, 3)
            )

            with self.frame_lock:
                self.latest_frame = frame
                self.frame_count += 1
                self.latest_sequence += 1
                self.last_frame_time = time.monotonic()

            if not first_frame_reported:
                self.get_logger().info(
                    f'First BGR frame received: {frame.shape}'
                )
                first_frame_reported = True

    def _publish_latest_frame(self):
        now = time.monotonic()

        with self.frame_lock:
            if (
                self.latest_frame is None
                or self.last_frame_time is None
            ):
                return

            frame_age = now - self.last_frame_time

            if frame_age > self.stale_timeout_sec:
                return

            if self.latest_sequence == self.last_published_sequence:
                return

            frame = self.latest_frame.copy()
            sequence = self.latest_sequence

        msg = self.bridge.cv2_to_imgmsg(
            frame,
            encoding='bgr8',
        )

        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id

        self.image_publisher.publish(msg)

        self.last_published_sequence = sequence
        self.published_count += 1

    def _check_stale_state(self):
        now = time.monotonic()

        with self.frame_lock:
            last_frame_time = self.last_frame_time

        if last_frame_time is None:
            is_stale = True
            frame_age = None
        else:
            frame_age = now - last_frame_time
            is_stale = frame_age > self.stale_timeout_sec

        if is_stale == self.stream_stale:
            return

        self.stream_stale = is_stale

        if is_stale:
            self.get_logger().warning(
                'STALE_FRAME detected: '
                f'age={frame_age:.3f}s '
                f'threshold={self.stale_timeout_sec:.3f}s; '
                'image publishing blocked'
            )
        else:
            self.get_logger().info(
                'Frame stream active/recovered; '
                'image publishing allowed'
            )

    def _publish_status(self):
        elapsed = time.monotonic() - self.start_time

        with self.frame_lock:
            frame_count = self.frame_count
            last_frame_time = self.last_frame_time

        capture_fps = (
            frame_count / elapsed if elapsed > 0.0 else 0.0
        )
        publish_fps = (
            self.published_count / elapsed if elapsed > 0.0 else 0.0
        )

        if last_frame_time is None:
            camera_status = "NO_FRAME"
            stale = True
            frame_age = None
        else:
            frame_age = time.monotonic() - last_frame_time
            stale = frame_age > self.stale_timeout_sec
            camera_status = "STALE" if stale else "ACTIVE"

        payload = {
            "camera_id": self.frame_id,
            "status": camera_status,
            "stale": stale,
            "capture_fps": round(capture_fps, 2),
            "publish_fps": round(publish_fps, 2),
            "last_frame_age_sec": (
                round(frame_age, 3)
                if frame_age is not None
                else None
            ),
            "reconnect_count": self.reconnect_count,
        }

        msg = String()
        msg.data = json.dumps(payload, separators=(",", ":"))
        self.status_publisher.publish(msg)

    def _print_status(self):
        elapsed = time.monotonic() - self.start_time

        if elapsed <= 0.0:
            return

        with self.frame_lock:
            frame_count = self.frame_count
            last_frame_time = self.last_frame_time

        capture_fps = frame_count / elapsed
        publish_fps = self.published_count / elapsed

        if last_frame_time is None:
            self.get_logger().warning(
                'No frame received yet'
            )
            return

        frame_age = time.monotonic() - last_frame_time

        stale = frame_age > self.stale_timeout_sec

        self.get_logger().info(
            f'frames={frame_count} '
            f'capture_fps={capture_fps:.2f} '
            f'published={self.published_count} '
            f'publish_fps={publish_fps:.2f} '
            f'last_frame_age={frame_age:.3f}s '
            f'stale={stale}'
        )

    def destroy_node(self):
        self.running = False
        self.stop_event.set()

        self._stop_ffmpeg()

        if self.capture_thread is not None:
            self.capture_thread.join(timeout=2.0)

        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)

    node = None

    try:
        node = GlobalCameraNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
