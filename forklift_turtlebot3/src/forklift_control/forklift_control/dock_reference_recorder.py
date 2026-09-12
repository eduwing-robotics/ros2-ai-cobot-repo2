#!/usr/bin/env python3
"""Average ArUco measurements at a manually confirmed docking pose."""

import math
import statistics

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

from forklift_interfaces.msg import MarkerDetection


class DockReferenceRecorder(Node):
    def __init__(self):
        super().__init__('dock_reference_recorder')
        self.declare_parameter('marker_id', 40)
        self.declare_parameter('sample_count', 60)
        self.marker_id = int(self.get_parameter('marker_id').value)
        self.sample_count = int(self.get_parameter('sample_count').value)
        self.forward = []
        self.lateral = []
        self.yaw = []
        self.done = False
        self.create_subscription(
            MarkerDetection,
            '/forklift/aruco/detection',
            self.on_detection,
            qos_profile_sensor_data,
        )
        self.get_logger().info(
            f'ArUco {self.marker_id} 도킹 완료 자세에서 '
            f'{self.sample_count}개 샘플을 수집합니다. 로봇을 움직이지 마세요.'
        )

    def on_detection(self, msg):
        if self.done or not msg.visible or msg.marker_id != self.marker_id:
            return
        self.forward.append(float(msg.forward_m))
        self.lateral.append(float(msg.lateral_m))
        self.yaw.append(float(msg.yaw_error_rad))
        if len(self.forward) < self.sample_count:
            return

        forward_mean = statistics.fmean(self.forward)
        lateral_mean = statistics.fmean(self.lateral)
        sin_mean = statistics.fmean(math.sin(value) for value in self.yaw)
        cos_mean = statistics.fmean(math.cos(value) for value in self.yaw)
        yaw_mean = math.atan2(sin_mean, cos_mean)
        forward_std = statistics.pstdev(self.forward)
        lateral_std = statistics.pstdev(self.lateral)
        yaw_std = statistics.pstdev(self.yaw)

        self.get_logger().info(
            '\n도킹 기준 측정 완료\n'
            f'  marker_{self.marker_id}_target_forward_m: {forward_mean:.5f}\n'
            f'  marker_{self.marker_id}_target_lateral_m: {lateral_mean:.5f}\n'
            f'  marker_{self.marker_id}_target_yaw_rad: {yaw_mean:.5f}\n'
            '측정 흔들림(표준편차)\n'
            f'  forward={forward_std:.5f}m, lateral={lateral_std:.5f}m, '
            f'yaw={math.degrees(yaw_std):.3f}deg'
        )
        self.done = True
        rclpy.shutdown()


def main(args=None):
    rclpy.init(args=args)
    node = DockReferenceRecorder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
