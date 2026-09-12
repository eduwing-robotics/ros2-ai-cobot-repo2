#!/usr/bin/env python3
"""Validate the editable calibration YAML before real robot launch."""

import os
import sys

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node


class CalibrationValidator(Node):
    def __init__(self):
        super().__init__('forklift_calibration_validator')
        self.declare_parameter('calibration_file', '')
        self.declare_parameter('camera_calibrated', False)
        self.declare_parameter('docking_targets_measured', False)
        self.declare_parameter('lift_presets_measured', False)

    def validate(self):
        path = str(self.get_parameter('calibration_file').value)
        if not path:
            path = os.path.join(
                get_package_share_directory('forklift_control'),
                'config',
                'calibration.yaml',
            )
        try:
            with open(path, encoding='utf-8') as stream:
                data = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError) as exc:
            self.get_logger().error(f'calibration 파일을 읽을 수 없습니다: {exc}')
            return False

        def params(node_name):
            section = data.get(node_name, {})
            return section.get('ros__parameters', {}) if isinstance(section, dict) else {}

        detector = params('aruco_detector')
        docking = params('aruco_docking')
        lift = params('forklift_lift_servo')
        flags = params('forklift_calibration_validator')

        errors = []
        if not bool(flags.get('camera_calibrated', False)):
            errors.append('카메라 캘리브레이션 확인 표시가 false입니다')
        if not bool(flags.get('docking_targets_measured', False)):
            errors.append('ArUco 40~42 도킹 목표 실측 표시가 false입니다')
        if not bool(flags.get('lift_presets_measured', False)):
            errors.append('HEIGHT_2/HEIGHT_3/PARK 프리셋 실측 표시가 false입니다')

        for name in ('marker_size_m', 'marker_42_size_m'):
            value = detector.get(name, 0.0)
            if not isinstance(value, (int, float)) or value <= 0.0:
                errors.append(f'{name}은 0보다 커야 합니다')
        for marker_id in (40, 41, 42):
            for suffix in ('target_forward_m', 'staging_forward_m'):
                name = f'marker_{marker_id}_{suffix}'
                value = docking.get(name, 0.0)
                if not isinstance(value, (int, float)) or value <= 0.0:
                    errors.append(f'{name}은 0보다 커야 합니다')
        home_forward = docking.get('home_marker_41_target_forward_m', 0.0)
        if not isinstance(home_forward, (int, float)) or home_forward <= 0.0:
            errors.append('home_marker_41_target_forward_m은 0보다 커야 합니다')

        for name in ('height_2_preset', 'height_3_preset', 'park_preset'):
            value = lift.get(name)
            if value not in (1, 2, 3, 4):
                errors.append(f'{name}은 1~4 중 하나여야 합니다')
        if lift.get('height_2_preset') == lift.get('height_3_preset'):
            errors.append('height_2_preset과 height_3_preset은 달라야 합니다')
        if lift.get('park_preset') != lift.get('height_2_preset'):
            errors.append('park_preset은 HEIGHT_2 프리셋과 같아야 합니다')

        self.get_logger().info(f'검사 파일: {path}')
        for message in errors:
            self.get_logger().error(f'[미완료] {message}')
        if errors:
            self.get_logger().error(f'실물 실행 준비 미완료: {len(errors)}개 항목')
            return False
        self.get_logger().info('실물 실행용 calibration 검사 통과')
        return True


def main(args=None):
    rclpy.init(args=args)
    node = CalibrationValidator()
    ok = node.validate()
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
