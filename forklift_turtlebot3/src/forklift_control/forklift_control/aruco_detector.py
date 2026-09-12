#!/usr/bin/env python3
"""Detect ArUco 40-44 from the TurtleBot camera and publish metric pose errors."""

import math

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image

from forklift_interfaces.msg import MarkerDetection


class ArucoDetectorNode(Node):
    def __init__(self):
        super().__init__('aruco_detector')
        defaults = (
            ('image_topic', '/camera/image_raw'),
            ('camera_info_topic', '/camera/camera_info'),
            ('debug_image_topic', '/forklift/aruco/debug_image'),
            ('fallback_camera_matrix', [
                641.16387, 0.0, 158.74866,
                0.0, 643.87291, 130.36306,
                0.0, 0.0, 1.0,
            ]),
            ('fallback_distortion_coefficients', [
                0.128834, -0.065037, 0.008062, 0.009493, 0.0,
            ]),
            ('dictionary', 'DICT_4X4_50'),
            ('marker_size_m', 0.05),
            ('marker_42_size_m', 0.05),
            ('allowed_marker_ids', [40, 41, 42]),
            ('minimum_perimeter_px', 40.0),
            ('debug_publish_hz', 5.0),
            ('lateral_sign', -1.0),
            ('yaw_sign', 1.0),
        )
        for name, value in defaults:
            self.declare_parameter(name, value)

        dictionary_name = str(self.get_parameter('dictionary').value)
        if not hasattr(cv2.aruco, dictionary_name):
            raise ValueError(f'지원하지 않는 ArUco dictionary: {dictionary_name}')
        dictionary_id = getattr(cv2.aruco, dictionary_name)
        self.dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        if hasattr(cv2.aruco, 'ArucoDetector'):
            self.detector_parameters = cv2.aruco.DetectorParameters()
            self.detector = cv2.aruco.ArucoDetector(
                self.dictionary, self.detector_parameters
            )
        else:
            # OpenCV 4.6 on ROS 2 Jazzy/aarch64 exposes the constructor but
            # its object can crash detectMarkers(). Use the legacy factory
            # with the matching legacy detection API instead.
            self.detector_parameters = cv2.aruco.DetectorParameters_create()
            self.detector = None

        self.bridge = CvBridge()
        fallback_matrix = list(
            self.get_parameter('fallback_camera_matrix').value
        )
        fallback_distortion = list(
            self.get_parameter('fallback_distortion_coefficients').value
        )
        self.camera_matrix = (
            np.asarray(fallback_matrix, dtype=np.float64).reshape(3, 3)
            if len(fallback_matrix) == 9
            else None
        )
        self.distortion = (
            np.asarray(fallback_distortion, dtype=np.float64)
            if fallback_distortion
            else np.zeros(5, dtype=np.float64)
        )
        self.allowed_ids = {
            int(value) for value in self.get_parameter('allowed_marker_ids').value
        }
        self.marker_size = float(self.get_parameter('marker_size_m').value)
        self.marker_42_size = float(
            self.get_parameter('marker_42_size_m').value
        )
        self.last_calibration_warning_ns = 0
        self.last_debug_publish_ns = 0

        self.detection_pub = self.create_publisher(
            MarkerDetection, '/forklift/aruco/detection', 10
        )
        self.debug_pub = self.create_publisher(
            Image,
            str(self.get_parameter('debug_image_topic').value),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            CameraInfo,
            str(self.get_parameter('camera_info_topic').value),
            self.on_camera_info,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Image,
            str(self.get_parameter('image_topic').value),
            self.on_image,
            qos_profile_sensor_data,
        )

    @staticmethod
    def make_object_points(marker_size):
        half = marker_size / 2.0
        return np.array(
            [
                [-half, half, 0.0],
                [half, half, 0.0],
                [half, -half, 0.0],
                [-half, -half, 0.0],
            ],
            dtype=np.float32,
        )

    def on_camera_info(self, msg):
        matrix = np.asarray(msg.k, dtype=np.float64).reshape(3, 3)
        if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
            return
        self.camera_matrix = matrix
        self.distortion = np.asarray(msg.d, dtype=np.float64)

    def on_image(self, msg):
        try:
            frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as exc:
            self.get_logger().error(f'카메라 영상 변환 실패: {exc}')
            return

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.detector is not None:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(
                gray, self.dictionary, parameters=self.detector_parameters
            )

        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame, corners, ids)
            for marker_corners, marker_id_array in zip(corners, ids):
                marker_id = int(marker_id_array[0])
                if marker_id not in self.allowed_ids:
                    continue
                points = np.asarray(marker_corners, dtype=np.float32).reshape(4, 2)
                perimeter = float(cv2.arcLength(points.reshape(-1, 1, 2), True))
                if perimeter < float(
                    self.get_parameter('minimum_perimeter_px').value
                ):
                    continue
                if self.camera_matrix is None:
                    self.warn_missing_calibration()
                    continue

                marker_size = (
                    self.marker_42_size if marker_id == 42 else self.marker_size
                )
                object_points = self.make_object_points(marker_size)

                solved, rvec, tvec = cv2.solvePnP(
                    object_points,
                    points,
                    self.camera_matrix,
                    self.distortion,
                    flags=cv2.SOLVEPNP_IPPE_SQUARE,
                )
                if not solved:
                    continue
                translation = tvec.reshape(3)
                rotation, _ = cv2.Rodrigues(rvec)
                normal = rotation[:, 2]
                yaw = math.atan2(float(normal[0]), float(-normal[2]))

                detection = MarkerDetection()
                detection.marker_id = marker_id
                detection.visible = True
                detection.lateral_m = float(
                    self.get_parameter('lateral_sign').value
                ) * float(translation[0])
                detection.forward_m = float(translation[2])
                detection.yaw_error_rad = float(
                    self.get_parameter('yaw_sign').value
                ) * yaw
                self.detection_pub.publish(detection)

                cv2.drawFrameAxes(
                    frame,
                    self.camera_matrix,
                    self.distortion,
                    rvec,
                    tvec,
                    marker_size * 0.5,
                )
                center = np.mean(points, axis=0).astype(int)
                label = (
                    f'ID {marker_id}  x={detection.lateral_m:+.3f}m '
                    f'z={detection.forward_m:.3f}m yaw={math.degrees(yaw):+.1f}deg'
                )
                cv2.putText(
                    frame,
                    label,
                    (max(0, int(center[0]) - 180), max(20, int(center[1]) - 25)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 255, 255),
                    1,
                    cv2.LINE_AA,
                )

        # Debug images are diagnostic only. Avoid converting and publishing a
        # full image when nobody is watching, and throttle remote viewers so a
        # slow Wi-Fi subscriber cannot stall the detection callback.
        if self.debug_pub.get_subscription_count() > 0:
            now_ns = self.get_clock().now().nanoseconds
            debug_hz = max(
                0.1, float(self.get_parameter('debug_publish_hz').value)
            )
            debug_period_ns = int(1_000_000_000 / debug_hz)
            if now_ns - self.last_debug_publish_ns >= debug_period_ns:
                debug_msg = self.bridge.cv2_to_imgmsg(frame, encoding='bgr8')
                debug_msg.header = msg.header
                self.debug_pub.publish(debug_msg)
                self.last_debug_publish_ns = now_ns

    def warn_missing_calibration(self):
        now = self.get_clock().now().nanoseconds
        if now - self.last_calibration_warning_ns > 5_000_000_000:
            self.get_logger().warning(
                '/camera/camera_info가 없거나 유효하지 않아 거리 계산을 건너뜁니다'
            )
            self.last_calibration_warning_ns = now


def main(args=None):
    rclpy.init(args=args)
    node = ArucoDetectorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
