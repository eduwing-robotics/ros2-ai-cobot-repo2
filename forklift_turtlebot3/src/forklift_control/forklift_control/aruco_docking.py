#!/usr/bin/env python3
"""Closed-loop ArUco docking and odometry-based straight backoff."""

import math

import rclpy
from geometry_msgs.msg import TwistStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

from forklift_interfaces.msg import DockCommand, DockState, MarkerDetection


def clamp(value, low, high):
    return max(low, min(high, value))


def wrap_angle(value):
    return math.atan2(math.sin(value), math.cos(value))


def quaternion_yaw(quaternion):
    siny = 2.0 * (
        quaternion.w * quaternion.z + quaternion.x * quaternion.y
    )
    cosy = 1.0 - 2.0 * (
        quaternion.y * quaternion.y + quaternion.z * quaternion.z
    )
    return math.atan2(siny, cosy)


class ArucoDocking(Node):
    def __init__(self):
        super().__init__('aruco_docking')
        defaults = (
            ('control_hz', 20.0),
            ('distance_tolerance_m', 0.012),
            ('lateral_tolerance_m', 0.012),
            ('yaw_tolerance_rad', 0.052),
            ('linear_kp', 0.55),
            ('heading_kp', 0.60),
            ('yaw_kp', 0.40),
            ('max_linear_mps', 0.02),
            ('min_linear_mps', 0.012),
            ('max_angular_rps', 0.08),
            ('detection_filter_alpha', 0.20),
            ('search_angular_rps', 0.0),
            ('search_command_angular_rps', 0.08),
            ('marker_42_search_angular_rps', 0.15),
            ('home_search_command_angular_rps', -0.20),
            ('home_search_fast_turn_rad', 2.62),
            ('home_search_initial_pause_sec', 2.0),
            ('home_search_step_rad', 0.262),
            ('home_search_step_angular_rps', -0.10),
            ('home_search_step_pause_sec', 1.0),
            ('home_search_acquire_stable_frames', 3),
            ('search_timeout_sec', 240.0),
            ('home_marker_id', 41),
            ('home_marker_41_target_forward_m', 0.66753),
            ('home_marker_41_target_lateral_m', 0.03151),
            ('home_marker_41_target_yaw_rad', 0.02721),
            ('home_distance_tolerance_m', 0.020),
            ('home_lateral_tolerance_m', 0.015),
            ('home_yaw_tolerance_rad', 0.140),
            # HOME 전진은 마커 41이 계속 보일 때만 허용한다. 검출이 잠깐만
            # 끊겨도 정지하며, 잘못된 원거리 검출과 무제한 주행을 막는다.
            ('home_detection_timeout_sec', 0.30),
            ('home_max_visible_marker_forward_m', 2.00),
            ('home_max_visible_forward_travel_m', 1.00),
            # 비교 시험용 빠른 HOME 정렬. 안전한 근거리에서만 전진과
            # 조향을 동시에 수행하고, 최대 전진거리를 odom으로 제한한다.
            ('home_fast_forward_stop_error_m', 0.020),
            ('home_fast_lateral_tolerance_m', 0.015),
            ('home_fast_max_angular_rps', 0.100),
            ('home_recovery_backoff_m', 0.050),
            ('home_recovery_backoff_speed_mps', 0.012),
            ('home_recovery_yaw_tolerance_rad', 0.035),
            ('home_recovery_yaw_stable_cycles', 5),
            ('home_recovery_max_count', 2),
            ('home_recovery_timeout_sec', 10.0),
            # GO_HOME 도착 뒤에는 고정 거리 복구 대신 마커 41의 거리
            # 오차 부호에 따라 제한된 속도로 전진/후진하며 HOME에 수렴한다.
            ('home_correction_min_linear_mps', 0.008),
            ('home_correction_max_linear_mps', 0.020),
            ('home_correction_max_reverse_m', 0.050),
            ('home_stable_cycles', 3),
            ('home_detection_loss_wait_sec', 15.0),
            ('home_max_linear_mps', 0.040),
            ('home_min_linear_mps', 0.020),
            ('home_max_angular_rps', 0.120),
            ('home_side_move_gain', 0.50),
            ('home_side_move_max_m', 0.015),
            ('home_side_move_max_count', 4),
            ('home_lateral_direction_sign', 1.0),
            ('home_side_rotate_speed_rps', 0.25),
            ('home_side_drive_speed_mps', 0.025),
            ('detection_timeout_sec', 1.0),
            ('final_insert_detection_grace_sec', 5.0),
            ('lost_after_seen_abort_sec', 15.0),
            ('final_lost_recovery_speed_mps', 0.015),
            ('final_lost_recovery_timeout_sec', 12.0),
            ('final_lost_recovery_max_m', 0.12),
            ('overall_timeout_sec', 600.0),
            ('stable_cycles', 10),
            ('reposition_trigger_forward_error_m', 0.05),
            ('reposition_lateral_threshold_m', 0.012),
            ('reposition_yaw_threshold_rad', 0.052),
            ('reposition_forward_margin_m', 0.12),
            ('backoff_rack_distance_m', 0.20),
            ('backoff_drop_distance_m', 0.20),
            ('backoff_home_distance_m', 0.20),
            ('backoff_speed_mps', 0.03),
            ('backoff_yaw_kp', 1.2),
            ('backoff_timeout_sec', 60.0),
            ('final_insert_distance_m', 0.0),
            ('final_insert_speed_mps', 0.015),
            ('final_insert_timeout_sec', 180.0),
            ('final_marker_tolerance_m', 0.005),
            ('rack_final_marker_tolerance_m', 0.010),
            ('marker_41_final_forward_max_m', 0.210),
            ('marker_42_final_insert_extension_m', 0.030),
            ('final_marker_stable_cycles', 5),
            ('final_insert_max_lateral_error_m', 0.025),
            ('final_insert_max_yaw_error_rad', 0.20),
            ('final_insert_wrong_direction_margin_m', 0.015),
            ('final_insert_marker_ids', [40, 41, 42]),
            # 새 5 cm Marker 42는 Rack1/2와 동일한 근거리 2단 도킹을 사용한다.
            # false로 바꾸면 기존 원거리 Marker 42 보정 로직으로 복귀한다.
            ('marker_42_use_rack_docking', True),
            ('final_handoff_forward_margin_m', 0.11),
            # 무회전 삽입 직전에는 포크가 팔레트 기둥을 향하지 않도록
            # staging보다 훨씬 엄격한 좌우 정렬을 요구한다.
            ('final_handoff_lateral_tolerance_m', 0.006),
            ('final_handoff_yaw_tolerance_rad', 0.14),
            # 아래 marker_42_* 값은 legacy mode(false) 롤백 전용이다.
            # handoff에서는 직진을 허용하고 최종 pose에서 다시 검증한다.
            ('marker_42_final_handoff_lateral_tolerance_m', 0.006),
            ('marker_42_final_handoff_yaw_tolerance_rad', 0.10),
            ('marker_42_final_max_lateral_error_m', 0.012),
            ('marker_42_final_lateral_tolerance_m', 0.010),
            ('marker_42_final_yaw_tolerance_rad', 0.087),
            ('marker_42_axis_rotate_deadband_rad', 0.05),
            ('marker_42_staging_lateral_tolerance_m', 0.015),
            ('marker_42_staging_yaw_tolerance_rad', 0.18),
            ('marker_42_staging_steering_deadband_rad', 0.03),
            ('marker_42_staging_reacquire_delay_sec', 1.0),
            ('marker_42_continuous_max_angular_rps', 0.03),
            ('marker_42_continuous_min_linear_mps', 0.008),
            # Rack1/2와 Drop 모두 정지 회전 반복 대신 저속 곡선으로
            # 팔레트 입구까지 시각 추종한다.
            ('continuous_visual_marker_ids', [40, 41, 42]),
            ('continuous_max_angular_rps', 0.03),
            ('continuous_min_linear_mps', 0.008),
            ('marker_42_lost_pause_sec', 1.5),
            ('marker_42_lost_backoff_m', 0.12),
            ('marker_42_direct_start_no_reverse', True),
            ('marker_42_handoff_forward_m', 0.27643),
            ('marker_42_handoff_lateral_m', 0.00169),
            ('marker_42_handoff_yaw_rad', 0.01449),
            ('marker_42_handoff_forward_tolerance_m', 0.006),
            ('marker_42_handoff_max_overshoot_m', 0.025),
            ('marker_42_handoff_lateral_tolerance_m', 0.006),
            ('marker_42_handoff_yaw_tolerance_rad', 0.12),
            ('marker_42_handoff_stable_cycles', 5),
            ('two_stage_marker_ids', [40, 41, 42]),
            ('staging_check_marker_ids', [40, 41, 42]),
            # Marker 42도 입구 오차가 남으면 팔레트와 떨어진 staging에서만
            # 최대 5 mm씩 측면 보정한다.
            ('staging_side_move_marker_ids', [40, 41, 42]),
            ('staging_forward_tolerance_m', 0.015),
            # Rack 입구가 받아주는 25 mm 이내 오차는 측면 왕복 보정하지 않는다.
            ('staging_lateral_tolerance_m', 0.025),
            ('staging_yaw_tolerance_rad', 0.14),
            ('staging_near_distance_m', 0.02),
            ('staging_side_move_max_m', 0.005),
            ('staging_side_rotate_speed_rps', 0.20),
            ('staging_side_drive_speed_mps', 0.02),
            ('staging_side_turn_tolerance_rad', 0.035),
            ('staging_lateral_direction_sign', 1.0),
            # Marker 42 실물 검증: dl 음수일 때 우측으로 보정한다.
            ('marker_42_staging_lateral_direction_sign', 1.0),
            ('marker_42_staging_side_move_gain', 1.0),
            ('marker_42_staging_side_move_max_m', 0.005),
            ('marker_42_staging_retry_backoff_m', 0.12),
            ('marker_42_staging_retry_speed_mps', 0.02),
            ('marker_42_staging_retry_max_count', 3),
            ('staging_steering_deadband_rad', 0.10),
            ('staging_min_forward_m', 0.20),
            ('staging_max_forward_m', 0.80),
            # Drop 42는 카메라에서 약 1.57 m 떨어진 원격 기준 마커다.
            ('marker_42_staging_min_forward_m', 1.00),
            ('marker_42_staging_max_forward_m', 2.00),
            # HOME_BEHIND START_SEARCH 전용 최초 획득 범위.
            ('marker_42_search_min_forward_m', 0.80),
            ('marker_42_search_max_forward_m', 2.50),
            ('marker_42_search_max_lateral_error_m', 0.70),
            ('marker_42_search_max_yaw_error_rad', 0.70),
            ('marker_42_acquire_max_lateral_error_m', 0.08),
            ('marker_42_acquire_max_yaw_error_rad', 0.35),
            ('marker_42_acquire_stable_frames', 5),
            ('staging_max_lateral_error_m', 0.20),
            ('staging_max_yaw_error_rad', 0.52),
            ('axis_rotate_deadband_rad', 0.025),
            ('axis_rotate_min_rps', 0.035),
        )
        for marker_id in (40, 41, 42):
            defaults += (
                (f'marker_{marker_id}_target_forward_m', 0.16),
                (f'marker_{marker_id}_target_lateral_m', 0.0),
                (f'marker_{marker_id}_target_yaw_rad', 0.0),
                (f'marker_{marker_id}_staging_forward_m', 0.40),
                (f'marker_{marker_id}_staging_lateral_m', 0.0),
                (f'marker_{marker_id}_staging_yaw_rad', 0.0),
            )
        for name, value in defaults:
            self.declare_parameter(name, value)

        self.command = None
        self.mode = None
        self.detection = None
        self.filtered_forward = None
        self.filtered_lateral = None
        self.filtered_yaw = None
        self.last_detection_ns = 0
        self.started_ns = 0
        self.first_seen = False
        self.lost_started_ns = None
        self.lost_recovery_start = None
        self.lost_recovery_announced = False
        self.marker_42_direct_start = False
        self.stable_count = 0
        self.dock_phase = 'FINAL'
        self.repositioning = False
        self.staging_checked = False
        self.odom_pose = None
        self.backoff_start = None
        self.final_insert_start = None
        self.final_insert_target_distance = None
        self.final_marker_stable_count = 0
        self.final_insert_marker_forward_start = None
        self.side_move_direction = 0.0
        self.side_move_distance = 0.0
        self.side_move_yaw_start = None
        self.side_move_position_start = None
        self.side_move_resume_phase = 'STAGING'
        self.pending_final_lateral_error = 0.0
        self.staging_retry_count = 0
        self.home_side_move_count = 0
        self.home_recovery_count = 0
        self.home_recovery_start = None
        self.home_recovery_started_ns = 0
        self.home_recovery_yaw_stable_count = 0
        self.home_reverse_distance = 0.0
        self.home_reverse_last_pose = None
        self.home_reverse_announced = False
        self.marker_42_acquire_last_detection_ns = 0
        self.home_alignment = False
        self.home_fast_alignment = False
        self.home_forward_start_pose = None
        self.home_search_acquired = False
        self.home_search_detection_count = 0
        self.home_search_detection_stamp = 0
        self.home_search_phase = 'FAST_TURN'
        self.home_search_turn_start_yaw = None
        self.home_search_pause_started_ns = 0
        self.search_enabled = False

        self.cmd_pub = self.create_publisher(TwistStamped, '/cmd_vel', 10)
        self.state_pub = self.create_publisher(
            DockState, '/forklift/dock/state', 10
        )
        self.create_subscription(
            DockCommand, '/forklift/dock/command', self.on_command, 10
        )
        self.create_subscription(
            MarkerDetection,
            '/forklift/aruco/detection',
            self.on_detection,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Odometry, '/odom', self.on_odom, qos_profile_sensor_data
        )
        self.create_timer(
            1.0 / float(self.get_parameter('control_hz').value), self.control
        )

    def on_command(self, msg):
        command = msg.command.strip().upper()
        if command == 'STOP':
            self.finish('FAILED', '도킹/후진 중지 요청')
            return
        if command not in (
            'START', 'START_SEARCH', 'FINAL_ONLY', 'ALIGN_HOME', 'ALIGN_HOME_SEARCH',
            'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH',
            'BACKOFF_RACK', 'BACKOFF_DROP', 'BACKOFF_HOME',
        ):
            self.publish_state(
                msg.task_id, msg.marker_id, 'FAILED', f'지원하지 않는 명령: {command}'
            )
            return
        if (
            command in (
                'ALIGN_HOME', 'ALIGN_HOME_SEARCH',
                'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH',
            )
            and int(msg.marker_id) != int(self.get_parameter('home_marker_id').value)
        ):
            self.publish_state(
                msg.task_id, msg.marker_id, 'FAILED',
                'HOME 정렬은 설정된 HOME 마커만 사용할 수 있습니다',
            )
            return
        if self.command is not None:
            self.publish_state(
                msg.task_id, msg.marker_id, 'FAILED', '이미 도킹 제어 중입니다'
            )
            return

        self.command = msg
        self.mode = (
            'DOCK'
            if command in (
                'START', 'START_SEARCH', 'FINAL_ONLY',
                'ALIGN_HOME', 'ALIGN_HOME_SEARCH',
                'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH',
            )
            else command
        )
        self.home_alignment = command in (
            'ALIGN_HOME', 'ALIGN_HOME_SEARCH',
            'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH',
        )
        self.home_fast_alignment = command in (
            'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH'
        )
        self.home_forward_start_pose = (
            self.odom_pose if self.home_alignment else None
        )
        self.search_enabled = command in (
            'START_SEARCH', 'ALIGN_HOME_SEARCH', 'ALIGN_HOME_FAST_SEARCH'
        )
        # DROP에서 20 cm 이탈한 뒤 다시 진입하는 START는 이미 안전한
        # staging 위치에 있다. 첫 전진 전의 순간 검출 유실 때문에 더
        # 후진하지 않도록 START_SEARCH와 복구 동작을 분리한다.
        self.marker_42_direct_start = (
            command == 'START' and int(msg.marker_id) == 42
        )
        self.home_search_acquired = not (
            self.home_alignment and self.search_enabled
        )
        self.home_search_detection_count = 0
        self.home_search_detection_stamp = 0
        self.home_search_phase = 'FAST_TURN'
        self.home_search_turn_start_yaw = (
            self.odom_pose[2] if self.odom_pose is not None else None
        )
        self.home_search_pause_started_ns = 0
        self.detection = None
        self.filtered_forward = None
        self.filtered_lateral = None
        self.filtered_yaw = None
        self.last_detection_ns = 0
        self.started_ns = self.get_clock().now().nanoseconds
        self.first_seen = False
        self.lost_started_ns = None
        self.lost_recovery_start = None
        self.lost_recovery_announced = False
        self.stable_count = 0
        two_stage_ids = {
            int(value)
            for value in self.get_parameter('two_stage_marker_ids').value
        }
        self.dock_phase = (
            'STAGING'
            if (
                self.mode == 'DOCK'
                and not self.home_alignment
                and command != 'FINAL_ONLY'
                and int(msg.marker_id) in two_stage_ids
            )
            else 'FINAL'
        )
        self.repositioning = False
        self.staging_checked = (
            self.home_alignment or command == 'FINAL_ONLY'
        )
        self.backoff_start = None
        self.final_insert_start = None
        self.final_insert_target_distance = None
        self.final_marker_stable_count = 0
        self.final_insert_marker_forward_start = None
        self.side_move_direction = 0.0
        self.side_move_distance = 0.0
        self.side_move_yaw_start = None
        self.side_move_position_start = None
        self.side_move_resume_phase = 'STAGING'
        self.pending_final_lateral_error = 0.0
        self.staging_retry_count = 0
        self.home_side_move_count = 0
        self.home_recovery_count = 0
        self.home_recovery_start = None
        self.home_recovery_started_ns = 0
        self.home_recovery_yaw_stable_count = 0
        self.home_reverse_distance = 0.0
        self.home_reverse_last_pose = None
        self.home_reverse_announced = False
        self.marker_42_acquire_last_detection_ns = 0

        if self.mode.startswith('BACKOFF'):
            if self.odom_pose is None:
                self.finish('FAILED', '/odom을 받지 못해 후진할 수 없습니다')
                return
            self.backoff_start = self.odom_pose
            message = '직선 후진 이탈 시작'
        else:
            if self.home_alignment:
                message = (
                    f'ArUco {msg.marker_id} 빠른 HOME 곡선 정렬 시작'
                    if self.home_fast_alignment
                    else f'ArUco {msg.marker_id} HOME 정렬 시작'
                )
            elif command == 'FINAL_ONLY':
                message = f'ArUco {msg.marker_id} 최종 직선 접근 재개'
            elif self.search_enabled:
                message = f'ArUco {msg.marker_id} 회전 탐색 및 도킹 시작'
            else:
                message = (
                    f'ArUco {msg.marker_id} 1차 staging 도킹 시작'
                    if self.dock_phase == 'STAGING'
                    else f'ArUco {msg.marker_id} 탐색 및 도킹 시작'
                )
        self.publish_state(msg.task_id, msg.marker_id, 'RUNNING', message)

    def on_detection(self, msg):
        if self.command is None or self.mode not in ('DOCK', 'FINAL_INSERT'):
            return
        if msg.marker_id != self.command.marker_id:
            return
        self.detection = msg
        if not msg.visible:
            return
        was_lost = self.lost_started_ns is not None
        alpha = float(self.get_parameter('detection_filter_alpha').value)
        if self.filtered_forward is None or was_lost:
            self.filtered_forward = float(msg.forward_m)
            self.filtered_lateral = float(msg.lateral_m)
            self.filtered_yaw = float(msg.yaw_error_rad)
        else:
            self.filtered_forward += alpha * (
                float(msg.forward_m) - self.filtered_forward
            )
            self.filtered_lateral += alpha * (
                float(msg.lateral_m) - self.filtered_lateral
            )
            yaw_delta = wrap_angle(float(msg.yaw_error_rad) - self.filtered_yaw)
            self.filtered_yaw = wrap_angle(self.filtered_yaw + alpha * yaw_delta)
        self.last_detection_ns = self.get_clock().now().nanoseconds
        self.first_seen = True
        self.lost_started_ns = None
        self.lost_recovery_start = None
        self.lost_recovery_announced = False

    def on_odom(self, msg):
        position = msg.pose.pose.position
        self.odom_pose = (
            float(position.x),
            float(position.y),
            quaternion_yaw(msg.pose.pose.orientation),
        )

    def control(self):
        if self.command is None:
            return
        if (
            self.mode == 'DOCK'
            and self.dock_phase.startswith('STAGING_SIDE_')
        ):
            self.control_staging_side_move()
        elif self.mode == 'DOCK':
            self.control_docking()
        elif self.mode == 'FINAL_INSERT':
            self.control_final_insert()
        else:
            self.control_backoff()

    def control_docking(self):
        now = self.get_clock().now().nanoseconds
        elapsed = (now - self.started_ns) / 1e9
        if elapsed > float(self.get_parameter('overall_timeout_sec').value):
            self.finish('FAILED', '도킹 제한시간 초과')
            return
        if (
            self.search_enabled
            and not self.first_seen
            and elapsed > float(self.get_parameter('search_timeout_sec').value)
        ):
            self.finish('FAILED', '제한시간 내 ArUco 마커를 찾지 못했습니다')
            return

        age = (
            (now - self.last_detection_ns) / 1e9
            if self.last_detection_ns
            else math.inf
        )
        detection_timeout = float(self.get_parameter(
            'home_detection_timeout_sec'
            if self.home_alignment
            else 'detection_timeout_sec'
        ).value)
        if self.detection is None or not self.detection.visible or age > detection_timeout:
            self.stable_count = 0
            # HOME_BEHIND에서는 예상 방향까지 빠르게 회전한 뒤 정지해서
            # 카메라가 선명한 프레임을 얻도록 한다. 이후에는 15도씩
            # 회전하고 잠시 정지하는 방식으로 마커를 탐색한다.
            if (
                self.home_alignment
                and self.search_enabled
                and not self.home_search_acquired
            ):
                self.home_search_detection_count = 0
                self.home_search_detection_stamp = 0
                self.first_seen = False
                self.lost_started_ns = None
                self.control_home_step_search(now)
                return
            # START_SEARCH 중 안전한 획득 자세가 확인되기 전의 순간 검출은
            # 실제 도킹 시작으로 취급하지 않는다. 계속 회전 탐색한다.
            if self.search_enabled and not self.staging_checked:
                self.stable_count = 0
                self.marker_42_acquire_last_detection_ns = 0
                self.first_seen = False
                self.lost_started_ns = None
                self.lost_recovery_start = None
                self.lost_recovery_announced = False
                self.velocity(
                    0.0,
                    self.search_angular_velocity(),
                )
                return
            # HOME은 팔레트 삽입 위치가 아니다. 마커를 한 번 획득한 뒤
            # 검출이 끊기면 다시 회전해 시야를 벗어나지 말고 정지한 채
            # detector가 회복되기를 기다린다. 한 번도 보지 못한 경우만
            # 아래의 일반 search 분기에서 회전 탐색한다.
            if self.home_alignment and self.first_seen:
                if self.lost_started_ns is None:
                    self.lost_started_ns = now
                lost_time = (now - self.lost_started_ns) / 1e9
                self.velocity(0.0, 0.0)
                if not self.lost_recovery_announced:
                    self.publish_state(
                        self.command.task_id,
                        int(self.command.marker_id),
                        'RUNNING',
                        'HOME 정렬 중 마커 유실: 정지 후 재검출 대기',
                    )
                    self.lost_recovery_announced = True
                if lost_time > float(
                    self.get_parameter('home_detection_loss_wait_sec').value
                ):
                    self.finish(
                        'FAILED',
                        'HOME 정렬 중 ArUco 마커 재검출 시간 초과',
                    )
                return
            if (
                self.search_enabled
                and self.marker_42_legacy_mode(self.command.marker_id)
                and self.dock_phase == 'STAGING'
            ):
                if self.lost_started_ns is None:
                    self.lost_started_ns = now
                    self.velocity(0.0, 0.0)
                    return
                lost_time = (now - self.lost_started_ns) / 1e9
                if lost_time < float(
                    self.get_parameter(
                        'marker_42_staging_reacquire_delay_sec'
                    ).value
                ):
                    self.velocity(0.0, 0.0)
                    return
                self.detection = None
                self.filtered_forward = None
                self.filtered_lateral = None
                self.filtered_yaw = None
                self.last_detection_ns = 0
                self.first_seen = False
                self.staging_checked = False
                self.lost_started_ns = None
                self.lost_recovery_start = None
                self.lost_recovery_announced = False
                self.velocity(0.0, self.search_angular_velocity())
                self.publish_state(
                    self.command.task_id,
                    int(self.command.marker_id),
                    'RUNNING',
                    '42번 staging 마커 유실: 회전 탐색으로 재획득 시도',
                )
                return
            if self.first_seen:
                if self.lost_started_ns is None:
                    self.lost_started_ns = now
                    self.lost_recovery_start = self.odom_pose
                    self.lost_recovery_announced = False
                lost_time = (now - self.lost_started_ns) / 1e9
                recovery_distance = math.inf
                if self.odom_pose is not None and self.lost_recovery_start is not None:
                    recovery_distance = math.hypot(
                        self.odom_pose[0] - self.lost_recovery_start[0],
                        self.odom_pose[1] - self.lost_recovery_start[1],
                    )
                if int(self.command.marker_id) == 42:
                    pause = float(
                        self.get_parameter('marker_42_lost_pause_sec').value
                    )
                    if lost_time <= pause:
                        self.velocity(0.0, 0.0)
                        return
                    if (
                        self.marker_42_direct_start
                        and bool(self.get_parameter(
                            'marker_42_direct_start_no_reverse'
                        ).value)
                    ):
                        self.velocity(0.0, 0.0)
                        if not self.lost_recovery_announced:
                            self.publish_state(
                                self.command.task_id,
                                42,
                                'RUNNING',
                                '42번 직접 재도킹 중 마커 유실: '
                                '자동 후진 없이 재검출 대기',
                            )
                            self.lost_recovery_announced = True
                        if lost_time > float(self.get_parameter(
                            'lost_after_seen_abort_sec'
                        ).value):
                            self.finish(
                                'FAILED',
                                '42번 직접 재도킹 중 ArUco '
                                '마커 재검출 시간 초과',
                            )
                        return
                    backoff_limit = float(
                        self.get_parameter('marker_42_lost_backoff_m').value
                    )
                    if (
                        self.odom_pose is not None
                        and recovery_distance < backoff_limit
                    ):
                        if not self.lost_recovery_announced:
                            self.publish_state(
                                self.command.task_id,
                                42,
                                'RUNNING',
                                '42번 마커 유실: 직선 후진하며 시야 재확보',
                            )
                            self.lost_recovery_announced = True
                        self.velocity(
                            -float(self.get_parameter(
                                'final_lost_recovery_speed_mps'
                            ).value),
                            0.0,
                        )
                        return

                    self.detection = None
                    self.filtered_forward = None
                    self.filtered_lateral = None
                    self.filtered_yaw = None
                    self.last_detection_ns = 0
                    self.first_seen = False
                    self.staging_checked = False
                    self.marker_42_acquire_last_detection_ns = 0
                    self.stable_count = 0
                    self.dock_phase = 'STAGING'
                    # Start a fresh search window after recovery.
                    self.started_ns = now
                    self.lost_started_ns = None
                    self.lost_recovery_start = None
                    self.lost_recovery_announced = False
                    self.velocity(0.0, self.search_angular_velocity())
                    self.publish_state(
                        self.command.task_id,
                        42,
                        'RUNNING',
                        '42번 후진 시야 확보 완료: 회전 재탐색',
                    )
                    return

                can_reverse_recover = (
                    self.dock_phase == 'FINAL'
                    and self.odom_pose is not None
                    and lost_time <= float(
                        self.get_parameter('final_lost_recovery_timeout_sec').value
                    )
                    and recovery_distance < float(
                        self.get_parameter('final_lost_recovery_max_m').value
                    )
                )
                if can_reverse_recover:
                    if not self.lost_recovery_announced:
                        self.publish_state(
                            self.command.task_id,
                            int(self.command.marker_id),
                            'RUNNING',
                            '2차 접근 중 마커 유실: 직선 후진하며 재획득 시도',
                        )
                        self.lost_recovery_announced = True
                    self.velocity(
                        -float(
                            self.get_parameter(
                                'final_lost_recovery_speed_mps'
                            ).value
                        ),
                        0.0,
                    )
                else:
                    self.velocity(0.0, 0.0)
                    abort_time = float(
                        self.get_parameter('lost_after_seen_abort_sec').value
                    )
                    if lost_time > abort_time:
                        self.finish(
                            'FAILED',
                            '2차 접근 중 ArUco 마커 재획득 실패'
                            if self.dock_phase == 'FINAL'
                            else '접근 중 ArUco 마커를 잃었습니다',
                        )
            else:
                angular = (
                    self.search_angular_velocity()
                    if self.search_enabled
                    else float(self.get_parameter('search_angular_rps').value)
                )
                self.velocity(0.0, angular)
            return

        marker_id = int(self.command.marker_id)
        if (
            self.home_alignment
            and self.search_enabled
            and not self.home_search_acquired
        ):
            self.velocity(0.0, 0.0)
            if self.last_detection_ns != self.home_search_detection_stamp:
                self.home_search_detection_stamp = self.last_detection_ns
                self.home_search_detection_count += 1
            required = int(self.get_parameter(
                'home_search_acquire_stable_frames'
            ).value)
            if self.home_search_detection_count < required:
                return
            self.home_search_acquired = True
            self.publish_state(
                self.command.task_id,
                marker_id,
                'RUNNING',
                'HOME 마커 41 정지 상태 안정 검출 완료: 곡선 정렬 시작',
            )

        if not self.staging_checked:
            checked_ids = {
                int(value)
                for value in self.get_parameter(
                    'staging_check_marker_ids'
                ).value
            }
            if marker_id in checked_ids:
                staging_forward = float(
                    self.get_parameter(
                        f'marker_{marker_id}_staging_forward_m'
                    ).value
                )
                staging_lateral = float(
                    self.get_parameter(
                        f'marker_{marker_id}_staging_lateral_m'
                    ).value
                )
                staging_yaw = float(
                    self.get_parameter(
                        f'marker_{marker_id}_staging_yaw_rad'
                    ).value
                )
                forward_error = self.filtered_forward - staging_forward
                lateral_error = self.filtered_lateral - staging_lateral
                yaw_error = wrap_angle(self.filtered_yaw - staging_yaw)
                if marker_id == 42 and self.search_enabled:
                    min_forward = 0.20
                    max_forward = 0.90
                    max_lateral_error = float(self.get_parameter(
                        'marker_42_acquire_max_lateral_error_m'
                    ).value)
                    max_yaw_error = float(self.get_parameter(
                        'marker_42_acquire_max_yaw_error_rad'
                    ).value)
                elif self.marker_42_legacy_mode(marker_id):
                    min_forward = float(
                        self.get_parameter(
                            'marker_42_staging_min_forward_m'
                        ).value
                    )
                    max_forward = float(
                        self.get_parameter(
                            'marker_42_staging_max_forward_m'
                        ).value
                    )
                    max_lateral_error = float(
                        self.get_parameter('staging_max_lateral_error_m').value
                    )
                    max_yaw_error = float(
                        self.get_parameter('staging_max_yaw_error_rad').value
                    )
                else:
                    min_forward = float(
                        self.get_parameter('staging_min_forward_m').value
                    )
                    max_forward = float(
                        self.get_parameter('staging_max_forward_m').value
                    )
                    max_lateral_error = float(
                        self.get_parameter('staging_max_lateral_error_m').value
                    )
                    max_yaw_error = float(
                        self.get_parameter('staging_max_yaw_error_rad').value
                    )
                # Staging is a correction target, not a gate requiring exact
                # manual placement. Reject only clearly unsafe observations.
                safe = (
                    min_forward <= self.filtered_forward <= max_forward
                    and abs(lateral_error) <= max_lateral_error
                    and abs(yaw_error) <= max_yaw_error
                )
                if not safe:
                    if self.search_enabled:
                        # 뒤/옆에서 잠깐 잡힌 마커가 필터에 남아 정상 정면
                        # 검출을 방해하지 않도록 버리고 회전 탐색을 계속한다.
                        self.stable_count = 0
                        self.marker_42_acquire_last_detection_ns = 0
                        self.detection = None
                        self.filtered_forward = None
                        self.filtered_lateral = None
                        self.filtered_yaw = None
                        self.last_detection_ns = 0
                        self.first_seen = False
                        self.lost_started_ns = None
                        self.velocity(
                            0.0,
                            self.search_angular_velocity(),
                        )
                        return
                    self.finish(
                        'FAILED',
                        'ArUco 획득 안전 범위를 벗어나 도킹하지 않습니다 '
                        f'(df={forward_error:+.3f}m, '
                        f'dl={lateral_error:+.3f}m, '
                        f'dyaw={math.degrees(yaw_error):+.1f}deg)',
                    )
                    return
            if marker_id == 42 and self.search_enabled:
                if (
                    self.last_detection_ns
                    != self.marker_42_acquire_last_detection_ns
                ):
                    self.marker_42_acquire_last_detection_ns = (
                        self.last_detection_ns
                    )
                    self.stable_count += 1
                self.velocity(0.0, 0.0)
                required = int(self.get_parameter(
                    'marker_42_acquire_stable_frames'
                ).value)
                if self.stable_count < required:
                    return
                self.stable_count = 0
                self.publish_state(
                    self.command.task_id,
                    marker_id,
                    'RUNNING',
                    '42번 정면 안정 검출 완료: staging 접근 시작',
                )
            self.staging_checked = True

        if self.dock_phase == 'STAGING_RETRY_BACKOFF':
            retry_forward = (
                self.target(marker_id, 'staging_forward_m')
                + float(
                    self.get_parameter(
                        'marker_42_staging_retry_backoff_m'
                    ).value
                )
            )
            if self.filtered_forward >= retry_forward:
                self.dock_phase = 'STAGING'
                self.stable_count = 0
                self.velocity(0.0, 0.0)
                self.publish_state(
                    self.command.task_id,
                    marker_id,
                    'RUNNING',
                    '42번 재접근 거리 확보 완료: 방향 보정 후 staging 재접근',
                )
            else:
                self.velocity(
                    -float(
                        self.get_parameter(
                            'marker_42_staging_retry_speed_mps'
                        ).value
                    ),
                    0.0,
                )
            return

        if self.dock_phase == 'RETURN_TO_STAGING':
            staging_forward = self.target(marker_id, 'staging_forward_m')
            staging_tolerance = float(
                self.get_parameter('staging_forward_tolerance_m').value
            )
            if self.filtered_forward >= staging_forward - staging_tolerance:
                self.stable_count = 0
                self.velocity(0.0, 0.0)
                # Handoff에서 확정한 오차를 사용한다. 후진 중 원격 마커의
                # lateral 값이 흔들리면 보정 방향이 반대로 뒤집힐 수 있다.
                correction = self.pending_final_lateral_error
                marker_42 = marker_id == 42
                handoff_lateral = float(
                    self.get_parameter(
                        'marker_42_handoff_lateral_tolerance_m'
                        if marker_42
                        else 'final_handoff_lateral_tolerance_m'
                    ).value
                )
                if (
                    abs(correction) > handoff_lateral
                    and self.staging_side_move_enabled(marker_id)
                ):
                    direction_sign = float(
                        self.get_parameter(
                            'marker_42_staging_lateral_direction_sign'
                            if marker_42
                            else 'staging_lateral_direction_sign'
                        ).value
                    )
                    self.side_move_direction = math.copysign(
                        1.0, correction * direction_sign
                    )
                    side_move_gain = (
                        float(self.get_parameter(
                            'marker_42_staging_side_move_gain'
                        ).value) if marker_42 else 1.0
                    )
                    self.side_move_distance = min(
                        abs(correction) * side_move_gain,
                        float(
                            self.get_parameter(
                                'marker_42_staging_side_move_max_m'
                                if marker_42
                                else 'staging_side_move_max_m'
                            ).value
                        ),
                    )
                    self.side_move_yaw_start = self.odom_pose[2]
                    self.side_move_position_start = None
                    self.side_move_resume_phase = 'FINAL'
                    self.pending_final_lateral_error = 0.0
                    self.dock_phase = 'STAGING_SIDE_TURN1'
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '2차 재정렬: staging 후진 완료, '
                        f'{self.side_move_distance:.3f}m '
                        f'{"좌" if self.side_move_direction > 0 else "우"} '
                        '측면 이동 시작',
                    )
                else:
                    self.pending_final_lateral_error = 0.0
                    self.dock_phase = 'FINAL'
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '2차 재정렬: staging 후진 완료, 최종 접근 재시작',
                    )
            else:
                self.velocity(-0.02, 0.0)
            return

        target_suffix = (
            'staging_' if self.dock_phase == 'STAGING' else ''
        )
        target_forward = self.target(marker_id, f'{target_suffix}forward_m')
        target_lateral = self.target(marker_id, f'{target_suffix}lateral_m')
        target_yaw = self.target(marker_id, f'{target_suffix}yaw_rad')
        forward_error = self.filtered_forward - target_forward
        lateral_error = self.filtered_lateral - target_lateral
        yaw_error = wrap_angle(self.filtered_yaw - target_yaw)

        if self.dock_phase == 'STAGING':
            distance_tolerance = float(
                self.get_parameter('staging_forward_tolerance_m').value
            )
            lateral_tolerance = float(
                self.get_parameter(
                    'marker_42_staging_lateral_tolerance_m'
                    if self.marker_42_legacy_mode(marker_id)
                    else 'staging_lateral_tolerance_m'
                ).value
            )
            yaw_tolerance = float(
                self.get_parameter(
                    'marker_42_staging_yaw_tolerance_rad'
                    if self.marker_42_legacy_mode(marker_id)
                    else 'staging_yaw_tolerance_rad'
                ).value
            )
        elif self.home_alignment:
            distance_tolerance = float(
                self.get_parameter('home_distance_tolerance_m').value
            )
            lateral_tolerance = float(
                self.get_parameter(
                    'home_fast_lateral_tolerance_m'
                    if self.home_fast_alignment
                    else 'home_lateral_tolerance_m'
                ).value
            )
            yaw_tolerance = float(
                self.get_parameter('home_yaw_tolerance_rad').value
            )
        else:
            distance_tolerance = float(
                self.get_parameter('distance_tolerance_m').value
            )
            lateral_tolerance = float(
                self.get_parameter('lateral_tolerance_m').value
            )
            yaw_tolerance = float(
                self.get_parameter('yaw_tolerance_rad').value
            )
        forward_within = abs(forward_error) <= distance_tolerance
        if self.home_alignment:
            # HOME은 목표 거리와 측면 오차를 함께 확인한다. 목표를 지나치면
            # 정면을 다시 확인한 뒤 제한된 거리만 직선 후진해 재접근한다.
            forward_upper_tolerance = (
                float(self.get_parameter(
                    'home_fast_forward_stop_error_m'
                ).value)
                if self.home_fast_alignment
                else distance_tolerance
            )
            forward_within = (
                -distance_tolerance <= forward_error
                <= forward_upper_tolerance
            )
        within = (
            forward_within
            and abs(lateral_error) <= lateral_tolerance
            and abs(yaw_error) <= yaw_tolerance
        )

        if self.home_alignment and self.dock_phase.startswith('HOME_RECOVERY_'):
            self.control_home_recovery(
                now, marker_id, forward_error, lateral_error, yaw_error
            )
            return

        # Marker 42 is visually steered only until the fork first enters the
        # pallet opening.  From this measured handoff pose onward, angular
        # correction is prohibited and the remaining distance is straight.
        if marker_id == 42 and self.dock_phase == 'FINAL':
            handoff_forward = float(self.get_parameter(
                'marker_42_handoff_forward_m'
            ).value)
            handoff_lateral = float(self.get_parameter(
                'marker_42_handoff_lateral_m'
            ).value)
            handoff_yaw = float(self.get_parameter(
                'marker_42_handoff_yaw_rad'
            ).value)
            handoff_forward_error = self.filtered_forward - handoff_forward
            handoff_lateral_error = self.filtered_lateral - handoff_lateral
            handoff_yaw_error = wrap_angle(self.filtered_yaw - handoff_yaw)
            handoff_forward_tolerance = float(self.get_parameter(
                'marker_42_handoff_forward_tolerance_m'
            ).value)
            handoff_reached = (
                handoff_forward_error <= handoff_forward_tolerance
                and handoff_forward_error >= -float(self.get_parameter(
                    'marker_42_handoff_max_overshoot_m'
                ).value)
            )
            handoff_aligned = (
                handoff_reached
                and abs(handoff_lateral_error) <= float(self.get_parameter(
                    'marker_42_handoff_lateral_tolerance_m'
                ).value)
                and abs(handoff_yaw_error) <= float(self.get_parameter(
                    'marker_42_handoff_yaw_tolerance_rad'
                ).value)
            )
            if handoff_aligned:
                self.stable_count += 1
                self.velocity(0.0, 0.0)
                if self.stable_count >= int(self.get_parameter(
                    'marker_42_handoff_stable_cycles'
                ).value):
                    if self.odom_pose is None:
                        self.finish(
                            'FAILED',
                            '/odom을 받지 못해 42번 직선 삽입할 수 없습니다',
                        )
                        return
                    self.mode = 'FINAL_INSERT'
                    self.final_insert_start = self.odom_pose
                    self.final_insert_target_distance = max(
                        0.0,
                        self.filtered_forward
                        - self.target(marker_id, 'forward_m'),
                    )
                    self.final_insert_marker_forward_start = self.filtered_forward
                    self.started_ns = now
                    self.stable_count = 0
                    self.velocity(0.0, 0.0)
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '42번 팔레트 입구 정렬 완료: '
                        f'{self.final_insert_target_distance:.3f}m '
                        '무회전 직선 삽입 시작',
                    )
                return
            self.stable_count = 0
            # The fork has reached the pallet entrance but is not centered.
            # Never steer here: back out straight and retry while there is room.
            if handoff_forward_error <= handoff_forward_tolerance:
                maximum = int(self.get_parameter(
                    'marker_42_staging_retry_max_count'
                ).value)
                if self.staging_retry_count >= maximum:
                    self.finish(
                        'FAILED',
                        '42번 입구 중심 정렬 재시도 횟수 초과 '
                        f'(dl={handoff_lateral_error:+.3f}m, '
                        f'dyaw={math.degrees(handoff_yaw_error):+.1f}deg)',
                    )
                    return
                self.staging_retry_count += 1
                self.pending_final_lateral_error = handoff_lateral_error
                self.dock_phase = 'RETURN_TO_STAGING'
                self.velocity(0.0, 0.0)
                self.publish_state(
                    self.command.task_id,
                    marker_id,
                    'RUNNING',
                    '42번 팔레트 입구 중심 오차: '
                    'staging까지 직선 후진 후 곡선 재접근 '
                    f'({self.staging_retry_count}/{maximum}, '
                    f'df={handoff_forward_error:+.3f}m, '
                    f'dl={handoff_lateral_error:+.3f}m, '
                    f'dyaw={math.degrees(handoff_yaw_error):+.1f}deg)',
                )
                return
        if self.dock_phase == 'STAGING' and self.marker_42_legacy_mode(marker_id):
            # Staging 허용범위에 들어와도 최종 직선 경로의 좌우 안전폭을
            # 만족해야만 무회전 삽입을 시작한다.
            final_lateral_error = (
                self.filtered_lateral
                - self.target(marker_id, 'lateral_m')
            )
            within = (
                within
                and abs(final_lateral_error)
                <= float(
                    self.get_parameter(
                        'marker_42_final_max_lateral_error_m'
                    ).value
                )
            )

        # Marker 42 stays under visual steering all the way to the measured
        # pose.  If it reaches the pallet with a bad pose, back straight to
        # staging and retry instead of rotating while the fork is in contact.
        if (
            marker_id == 42
            and self.dock_phase == 'FINAL'
            and forward_error <= distance_tolerance
            and not within
        ):
            maximum = 2
            if self.staging_retry_count >= maximum:
                self.finish(
                    'FAILED',
                    '42번 연속 시각 정렬 재시도 횟수 초과 '
                    f'(dl={lateral_error:+.3f}m, '
                    f'dyaw={math.degrees(yaw_error):+.1f}deg)',
                )
                return
            self.staging_retry_count += 1
            self.pending_final_lateral_error = lateral_error
            self.dock_phase = 'RETURN_TO_STAGING'
            self.velocity(0.0, 0.0)
            self.publish_state(
                self.command.task_id,
                marker_id,
                'RUNNING',
                '42번 근접 자세 오차: staging까지 직선 후진 후 '
                '곡선 재접근 '
                f'({self.staging_retry_count}/{maximum}, '
                f'dl={lateral_error:+.3f}m, '
                f'dyaw={math.degrees(yaw_error):+.1f}deg)',
            )
            return

        # At the second-stage handoff, stop using steering near the pallet.
        # Validate ArUco alignment once, then finish with straight odom creep.
        if (
            self.dock_phase == 'FINAL'
            and not self.home_alignment
            and marker_id != 42
        ):
            handoff_margin = float(
                self.get_parameter('final_handoff_forward_margin_m').value
            )
            if forward_error <= handoff_margin and not within:
                handoff_lateral = float(
                    self.get_parameter(
                        'marker_42_final_handoff_lateral_tolerance_m'
                        if self.marker_42_legacy_mode(marker_id)
                        else 'final_handoff_lateral_tolerance_m'
                    ).value
                )
                handoff_yaw = float(
                    self.get_parameter(
                        'marker_42_final_handoff_yaw_tolerance_rad'
                        if self.marker_42_legacy_mode(marker_id)
                        else 'final_handoff_yaw_tolerance_rad'
                    ).value
                )
                if (
                    abs(lateral_error) > handoff_lateral
                    or abs(yaw_error) > handoff_yaw
                ):
                    self.pending_final_lateral_error = lateral_error
                    self.dock_phase = 'RETURN_TO_STAGING'
                    self.velocity(0.0, 0.0)
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '2차 handoff 정렬 범위 밖: '
                        'staging 위치까지 직선 후진 후 재시도 '
                        f'(dl={lateral_error:+.3f}m, '
                        f'dyaw={math.degrees(yaw_error):+.1f}deg)',
                    )
                    return

                if self.odom_pose is None:
                    self.finish(
                        'FAILED',
                        '/odom을 받지 못해 최종 직선 삽입할 수 없습니다',
                    )
                    return
                insert_ids = {
                    int(value)
                    for value in self.get_parameter(
                        'final_insert_marker_ids'
                    ).value
                }
                extra = (
                    float(self.get_parameter('final_insert_distance_m').value)
                    if marker_id in insert_ids
                    else 0.0
                )
                self.mode = 'FINAL_INSERT'
                self.final_insert_start = self.odom_pose
                self.final_insert_target_distance = (
                    max(0.0, forward_error) + extra
                )
                self.final_insert_marker_forward_start = self.filtered_forward
                self.started_ns = now
                self.velocity(0.0, 0.0)
                self.publish_state(
                    self.command.task_id,
                    marker_id,
                    'RUNNING',
                    '2차 ArUco 정렬 완료: '
                    f'{self.final_insert_target_distance:.3f}m '
                    '무회전 직선 삽입 시작',
                )
                return

        if within:
            self.stable_count += 1
            self.velocity(0.0, 0.0)
            stable_parameter = (
                'home_stable_cycles'
                if self.home_alignment
                else 'stable_cycles'
            )
            if self.stable_count >= int(
                self.get_parameter(stable_parameter).value
            ):
                if self.dock_phase == 'STAGING':
                    if self.marker_42_legacy_mode(marker_id):
                        if self.odom_pose is None:
                            self.finish(
                                'FAILED',
                                '/odom을 받지 못해 42번 직선 삽입할 수 없습니다',
                            )
                            return
                        insert_distance = max(
                            0.0,
                            self.target(marker_id, 'staging_forward_m')
                            - self.target(marker_id, 'forward_m'),
                        )
                        self.mode = 'FINAL_INSERT'
                        self.final_insert_start = self.odom_pose
                        self.final_insert_target_distance = insert_distance
                        self.final_insert_marker_forward_start = self.filtered_forward
                        self.started_ns = now
                        self.stable_count = 0
                        self.velocity(0.0, 0.0)
                        self.publish_state(
                            self.command.task_id,
                            marker_id,
                            'RUNNING',
                            '42번 staging 정렬 완료: '
                            f'{insert_distance:.3f}m 무회전 직선 삽입 시작',
                        )
                        return
                    self.dock_phase = 'FINAL'
                    self.stable_count = 0
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '1차 staging 정렬 완료: 2차 최종 ArUco 도킹 시작',
                    )
                    return
                if self.home_alignment:
                    elapsed_sec = (
                        self.get_clock().now().nanoseconds - self.started_ns
                    ) / 1e9
                    method = '빠른 곡선' if self.home_fast_alignment else '기존 단계별'
                    self.finish(
                        'SUCCEEDED',
                        f'{method} HOME ArUco 정렬 완료 '
                        f'({elapsed_sec:.1f}s, df={forward_error:+.3f}m, '
                        f'dl={lateral_error:+.3f}m, '
                        f'dyaw={math.degrees(yaw_error):+.1f}deg)',
                    )
                    return
                insert_ids = {
                    int(value)
                    for value in self.get_parameter(
                        'final_insert_marker_ids'
                    ).value
                }
                insert_distance = float(
                    self.get_parameter('final_insert_distance_m').value
                )
                if marker_id in insert_ids and insert_distance > 0.0:
                    if self.odom_pose is None:
                        self.finish(
                            'FAILED',
                            '/odom을 받지 못해 최종 삽입할 수 없습니다',
                        )
                        return
                    self.mode = 'FINAL_INSERT'
                    self.final_insert_start = self.odom_pose
                    self.final_insert_target_distance = insert_distance
                    self.final_insert_marker_forward_start = self.filtered_forward
                    self.started_ns = now
                    self.velocity(0.0, 0.0)
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        f'{insert_distance:.3f}m 최종 삽입 시작',
                    )
                else:
                    self.finish('SUCCEEDED', 'ArUco 정밀 도킹 완료')
            return
        self.stable_count = 0

        if self.home_alignment:
            # HOME은 먼저 마커 정면 방향과 목표 거리를 맞춘다. 좌우 오차는
            # 그 후에만 turn-drive-turn으로 절반씩 보정하고 매번 재측정한다.
            if self.home_fast_alignment and forward_error > float(
                self.get_parameter('home_fast_forward_stop_error_m').value
            ):
                if not self.home_visible_forward_safe():
                    return

                # Rack 접근처럼 전진과 조향을 동시에 수행하되 HOME에서는
                # 짧은 안전 구간에만 적용한다.
                heading_error = math.atan2(
                    lateral_error, max(self.filtered_forward, 0.10)
                )
                steering_error = (
                    float(self.get_parameter('heading_kp').value) * heading_error
                    + float(self.get_parameter('yaw_kp').value) * yaw_error
                )
                max_angular = float(self.get_parameter(
                    'home_fast_max_angular_rps'
                ).value)
                angular = clamp(steering_error, -max_angular, max_angular)
                maximum = float(self.get_parameter(
                    'home_correction_max_linear_mps'
                ).value)
                minimum = float(self.get_parameter(
                    'home_correction_min_linear_mps'
                ).value)
                linear = clamp(
                    float(self.get_parameter('linear_kp').value) * forward_error,
                    minimum,
                    maximum,
                )
                if max_angular > 0.0:
                    linear *= max(
                        0.40, 1.0 - 0.60 * abs(angular) / max_angular
                    )
                self.velocity(linear, angular)
                return

            if abs(yaw_error) > yaw_tolerance:
                max_angular = float(
                    self.get_parameter('home_max_angular_rps').value
                )
                min_angular = float(
                    self.get_parameter('axis_rotate_min_rps').value
                )
                angular = clamp(yaw_error, -max_angular, max_angular)
                if abs(angular) < min_angular:
                    angular = math.copysign(min_angular, angular)
                self.velocity(0.0, angular)
                return

            if abs(forward_error) > distance_tolerance:
                if forward_error < 0.0:
                    # 목표보다 가까우면 마커를 정면으로 맞춘 뒤에만
                    # 직선 후진한다. 비스듬한 상태의 후진은 금지한다.
                    tight_yaw = float(self.get_parameter(
                        'home_recovery_yaw_tolerance_rad'
                    ).value)
                    if abs(yaw_error) > tight_yaw:
                        self.home_reverse_last_pose = None
                        maximum_angular = float(self.get_parameter(
                            'home_max_angular_rps'
                        ).value)
                        minimum_angular = float(self.get_parameter(
                            'axis_rotate_min_rps'
                        ).value)
                        angular = clamp(
                            yaw_error, -maximum_angular, maximum_angular
                        )
                        if abs(angular) < minimum_angular:
                            angular = math.copysign(
                                minimum_angular, angular
                            )
                        self.velocity(0.0, angular)
                        return

                    if self.odom_pose is None:
                        self.finish(
                            'FAILED',
                            '/odom을 받지 못해 HOME 후진 보정할 수 없습니다',
                        )
                        return

                    if self.home_reverse_last_pose is not None:
                        last_x, last_y, _ = self.home_reverse_last_pose
                        self.home_reverse_distance += math.hypot(
                            self.odom_pose[0] - last_x,
                            self.odom_pose[1] - last_y,
                        )
                    self.home_reverse_last_pose = self.odom_pose

                    maximum_reverse = float(self.get_parameter(
                        'home_correction_max_reverse_m'
                    ).value)
                    if self.home_reverse_distance >= maximum_reverse:
                        self.finish(
                            'FAILED',
                            'HOME 후진 보정 최대거리 도달로 안전 정지 '
                            f'(odom={self.home_reverse_distance:.3f}/'
                            f'{maximum_reverse:.3f}m)',
                        )
                        return

                    maximum = float(self.get_parameter(
                        'home_correction_max_linear_mps'
                    ).value)
                    minimum = float(self.get_parameter(
                        'home_correction_min_linear_mps'
                    ).value)
                    linear = clamp(
                        float(self.get_parameter('linear_kp').value)
                        * forward_error,
                        -maximum,
                        0.0,
                    )
                    if abs(linear) < minimum:
                        linear = -minimum
                    if not self.home_reverse_announced:
                        self.publish_state(
                            self.command.task_id,
                            marker_id,
                            'RUNNING',
                            'HOME 목표 거리 보정: 마커 41을 보며 비례 후진',
                        )
                        self.home_reverse_announced = True
                    self.velocity(linear, 0.0)
                    return

                if not self.home_visible_forward_safe():
                    return
                self.home_reverse_last_pose = None
                self.home_reverse_announced = False
                maximum = float(self.get_parameter(
                    'home_correction_max_linear_mps'
                ).value)
                minimum = float(self.get_parameter(
                    'home_correction_min_linear_mps'
                ).value)
                linear = clamp(
                    float(self.get_parameter('linear_kp').value)
                    * forward_error,
                    0.0,
                    maximum,
                )
                if abs(linear) < minimum:
                    linear = minimum
                self.velocity(linear, 0.0)
                return

            self.home_reverse_last_pose = None
            self.home_reverse_announced = False

            if abs(lateral_error) > lateral_tolerance:
                # 빠른 곡선 접근에서 앞뒤 위치는 맞았지만 측면 오차만
                # 남았다면 5cm 후진 복구를 반복하지 않는다. 현재 위치에서
                # 기존의 절반값 turn-drive-turn 측면 보정으로 전환한다.
                if self.home_fast_alignment:
                    self.home_fast_alignment = False
                    self.velocity(0.0, 0.0)
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '빠른 HOME 접근 완료: 후진 없이 단계별 측면 보정 전환',
                    )
                    return
                if self.odom_pose is None:
                    self.finish(
                        'FAILED', '/odom을 받지 못해 HOME 측면 보정할 수 없습니다'
                    )
                    return
                maximum_count = int(
                    self.get_parameter('home_side_move_max_count').value
                )
                if self.home_side_move_count >= maximum_count:
                    self.finish(
                        'FAILED',
                        'HOME 측면 보정 재시도 횟수 초과 '
                        f'(dl={lateral_error:+.3f}m)',
                    )
                    return
                self.home_side_move_count += 1
                direction_sign = float(
                    self.get_parameter('home_lateral_direction_sign').value
                )
                self.side_move_direction = math.copysign(
                    1.0, lateral_error * direction_sign
                )
                self.side_move_distance = min(
                    abs(lateral_error)
                    * float(self.get_parameter('home_side_move_gain').value),
                    float(self.get_parameter('home_side_move_max_m').value),
                )
                self.side_move_yaw_start = self.odom_pose[2]
                self.side_move_position_start = None
                self.side_move_resume_phase = 'FINAL'
                self.dock_phase = 'STAGING_SIDE_TURN1'
                self.velocity(0.0, 0.0)
                self.publish_state(
                    self.command.task_id,
                    marker_id,
                    'RUNNING',
                    'HOME 측면 오차 절반 보정 '
                    f'({self.home_side_move_count}/{maximum_count}): '
                    f'{self.side_move_distance:.3f}m '
                    f'{"좌" if self.side_move_direction > 0 else "우"} 이동',
                )
                return

            self.velocity(0.0, 0.0)
            return

        if self.dock_phase == 'STAGING':
            near_distance = float(
                self.get_parameter('staging_near_distance_m').value
            )
            if abs(forward_error) <= near_distance and marker_id != 42:
                # Ignore small yaw noise; correct only a clearly bad heading.
                if abs(yaw_error) > yaw_tolerance:
                    max_angular = float(
                        self.get_parameter('max_angular_rps').value
                    )
                    min_angular = float(
                        self.get_parameter('axis_rotate_min_rps').value
                    )
                    angular = clamp(yaw_error, -max_angular, max_angular)
                    if abs(angular) < min_angular:
                        angular = math.copysign(min_angular, angular)
                    self.velocity(0.0, angular)
                    return

                # Rotation alone cannot change lateral position. Translate
                # sideways with a turn-drive-turn maneuver, then reacquire.
                side_correction = lateral_error
                side_required = abs(lateral_error) > lateral_tolerance
                if self.marker_42_legacy_mode(marker_id):
                    final_lateral_error = (
                        self.filtered_lateral
                        - self.target(marker_id, 'lateral_m')
                    )
                    final_lateral_limit = float(
                        self.get_parameter(
                            'marker_42_final_max_lateral_error_m'
                        ).value
                    )
                    if abs(final_lateral_error) > final_lateral_limit:
                        side_required = True
                        if not abs(lateral_error) > lateral_tolerance:
                            side_correction = final_lateral_error
                if side_required and self.staging_side_move_enabled(marker_id):
                    if self.marker_42_legacy_mode(marker_id):
                        maximum = int(
                            self.get_parameter(
                                'marker_42_staging_retry_max_count'
                            ).value
                        )
                        if self.staging_retry_count >= maximum:
                            self.finish(
                                'FAILED',
                                '42번 staging 정렬 재시도 횟수 초과 '
                                f'(dl={lateral_error:+.3f}m)',
                            )
                            return
                        self.staging_retry_count += 1
                        self.dock_phase = 'STAGING_RETRY_BACKOFF'
                        self.velocity(0.0, 0.0)
                        self.publish_state(
                            self.command.task_id,
                            marker_id,
                            'RUNNING',
                            '42번 staging 좌우 오차: '
                            '0.12m 후진 후 방향 보정 재접근 '
                            f'({self.staging_retry_count}/{maximum}, '
                            f'dl={lateral_error:+.3f}m)',
                        )
                        return
                    if self.odom_pose is None:
                        self.finish(
                            'FAILED',
                            '/odom을 받지 못해 staging 측면 이동할 수 없습니다',
                        )
                        return
                    direction_sign = float(
                        self.get_parameter(
                            'marker_42_staging_lateral_direction_sign'
                            if self.marker_42_legacy_mode(marker_id)
                            else 'staging_lateral_direction_sign'
                        ).value
                    )
                    self.side_move_direction = math.copysign(
                        1.0, side_correction * direction_sign
                    )
                    side_move_gain = (
                        float(self.get_parameter(
                            'marker_42_staging_side_move_gain'
                        ).value) if self.marker_42_legacy_mode(marker_id) else 1.0
                    )
                    self.side_move_distance = min(
                        abs(side_correction) * side_move_gain,
                        float(
                            self.get_parameter(
                                'marker_42_staging_side_move_max_m'
                                if self.marker_42_legacy_mode(marker_id)
                                else 'staging_side_move_max_m'
                            ).value
                        ),
                    )
                    self.side_move_yaw_start = self.odom_pose[2]
                    self.side_move_position_start = None
                    self.side_move_resume_phase = 'STAGING'
                    self.dock_phase = 'STAGING_SIDE_TURN1'
                    self.velocity(0.0, 0.0)
                    self.publish_state(
                        self.command.task_id,
                        marker_id,
                        'RUNNING',
                        '1차 staging 측면 위치 보정 시작: '
                        f'{self.side_move_distance:.3f}m '
                        f'{"좌" if self.side_move_direction > 0 else "우"} 이동',
                    )
                    return

        heading_error = math.atan2(lateral_error, max(forward_error, 0.05))
        continuous_visual = (
            not self.home_alignment
            and marker_id in {
                int(value)
                for value in self.get_parameter(
                    'continuous_visual_marker_ids'
                ).value
            }
        )
        if self.dock_phase == 'STAGING':
            # On staging approach, lateral bearing has priority. Yaw is
            # corrected separately at the safe staging distance.
            steering_error = (
                float(self.get_parameter('heading_kp').value) * heading_error
            )
        else:
            steering_error = (
                float(self.get_parameter('heading_kp').value) * heading_error
                + float(self.get_parameter('yaw_kp').value) * yaw_error
            )

        # Pallet markers use slow simultaneous translation and steering.
        # Other markers retain axis-only rotate-then-drive control.
        if self.dock_phase == 'STAGING':
            rotate_deadband_parameter = (
                'marker_42_staging_steering_deadband_rad'
                if marker_id == 42
                else 'staging_steering_deadband_rad'
            )
        elif self.marker_42_legacy_mode(marker_id):
            rotate_deadband_parameter = 'marker_42_axis_rotate_deadband_rad'
        else:
            rotate_deadband_parameter = 'axis_rotate_deadband_rad'
        rotate_deadband = float(
            self.get_parameter(rotate_deadband_parameter).value
        )
        angular = 0.0
        if abs(steering_error) > rotate_deadband:
            max_angular = float(
                self.get_parameter('max_angular_rps').value
            )
            min_angular = float(
                self.get_parameter('axis_rotate_min_rps').value
            )
            angular = clamp(steering_error, -max_angular, max_angular)
            if abs(angular) < min_angular and not continuous_visual:
                angular = math.copysign(min_angular, angular)
            if not continuous_visual:
                self.velocity(0.0, angular)
                return

        linear = clamp(
            float(self.get_parameter('linear_kp').value) * forward_error,
            -float(self.get_parameter('max_linear_mps').value),
            float(self.get_parameter('max_linear_mps').value),
        )
        minimum = float(self.get_parameter('min_linear_mps').value)
        if forward_error > distance_tolerance:
            linear = max(linear, minimum)
        elif forward_error < -distance_tolerance:
            linear = min(linear, -minimum)
        if continuous_visual:
            # A differential-drive robot changes lateral position only while
            # translating.  Keep creeping forward while steering instead of
            # alternating in-place turns and straight segments.
            linear = max(0.0, linear)
            max_angular = float(self.get_parameter(
                'continuous_max_angular_rps'
            ).value)
            angular = clamp(angular, -max_angular, max_angular)
            if max_angular > 0.0:
                linear *= max(
                    0.35,
                    1.0 - 0.65 * abs(angular) / max_angular,
                )
            if forward_error > distance_tolerance:
                linear = max(
                    linear,
                    float(self.get_parameter(
                        'continuous_min_linear_mps'
                    ).value),
                )
            self.velocity(linear, angular)
        else:
            self.velocity(linear, 0.0)

    def control_staging_side_move(self):
        if self.odom_pose is None:
            self.finish('FAILED', '/odom staging 측면 이동 기준을 잃었습니다')
            return
        now = self.get_clock().now().nanoseconds
        elapsed = (now - self.started_ns) / 1e9
        if elapsed > float(self.get_parameter('overall_timeout_sec').value):
            self.finish('FAILED', '도킹 제한시간 초과')
            return

        x, y, yaw = self.odom_pose
        turn_speed = float(
            self.get_parameter(
                'home_side_rotate_speed_rps'
                if self.home_alignment
                else 'staging_side_rotate_speed_rps'
            ).value
        )
        turn_tolerance = float(
            self.get_parameter('staging_side_turn_tolerance_rad').value
        )

        if self.dock_phase == 'STAGING_SIDE_TURN1':
            turned = wrap_angle(yaw - self.side_move_yaw_start)
            if (
                turned * self.side_move_direction
                >= math.pi / 2.0 - turn_tolerance
            ):
                self.side_move_position_start = (x, y)
                self.dock_phase = 'STAGING_SIDE_DRIVE'
                self.velocity(0.0, 0.0)
                return
            self.velocity(0.0, self.side_move_direction * turn_speed)
            return

        if self.dock_phase == 'STAGING_SIDE_DRIVE':
            if self.side_move_position_start is None:
                self.finish('FAILED', 'staging 측면 직진 시작점을 잃었습니다')
                return
            sx, sy = self.side_move_position_start
            travelled = math.hypot(x - sx, y - sy)
            if travelled >= self.side_move_distance:
                self.side_move_yaw_start = yaw
                self.dock_phase = 'STAGING_SIDE_TURN2'
                self.velocity(0.0, 0.0)
                return
            drive_parameter = (
                'home_side_drive_speed_mps'
                if self.home_alignment
                else 'staging_side_drive_speed_mps'
            )
            self.velocity(
                float(self.get_parameter(drive_parameter).value), 0.0
            )
            return

        if self.dock_phase == 'STAGING_SIDE_TURN2':
            turned = wrap_angle(yaw - self.side_move_yaw_start)
            if (
                turned * (-self.side_move_direction)
                >= math.pi / 2.0 - turn_tolerance
            ):
                resume_phase = self.side_move_resume_phase
                self.dock_phase = resume_phase
                self.side_move_resume_phase = 'STAGING'
                self.stable_count = 0
                self.detection = None
                self.filtered_forward = None
                self.filtered_lateral = None
                self.filtered_yaw = None
                self.last_detection_ns = 0
                # HOME 측면 이동 후에는 이미 정면을 복구했다. 검출을
                # 기다리는 동안 다시 회전하지 않도록 획득 상태를 유지한다.
                self.first_seen = self.home_alignment
                self.lost_started_ns = None
                self.velocity(0.0, 0.0)
                self.publish_state(
                    self.command.task_id,
                    self.command.marker_id,
                    'RUNNING',
                    (
                        'HOME 측면 이동 완료: 정면에서 ArUco 재관측'
                        if self.home_alignment
                        else (
                            '2차 측면 이동 완료: 최종 ArUco 접근 재시작'
                            if resume_phase == 'FINAL'
                            else '1차 staging 측면 이동 완료: ArUco 재관측'
                        )
                    ),
                )
                return
            self.velocity(0.0, -self.side_move_direction * turn_speed)

    def control_final_insert(self):
        if self.odom_pose is None or self.final_insert_start is None:
            self.finish('FAILED', '/odom 최종 삽입 기준을 잃었습니다')
            return

        now = self.get_clock().now().nanoseconds
        elapsed = (now - self.started_ns) / 1e9
        timeout = float(
            self.get_parameter('final_insert_timeout_sec').value
        )
        start_x, start_y, start_yaw = self.final_insert_start
        current_x, current_y, current_yaw = self.odom_pose
        travelled = math.hypot(current_x - start_x, current_y - start_y)
        target = self.final_insert_target_distance
        if target is None:
            target = float(
                self.get_parameter('final_insert_distance_m').value
            )
        marker_id = int(self.command.marker_id)
        # marker target은 실제 완전 도킹 자세에서 측정한 값이다. 6 mm는
        # handoff 지점에서 계산하는 직선 이동량에만 더하고, 성공 pose에서
        # 다시 빼지 않는다.
        desired_forward = self.target(marker_id, 'forward_m')
        marker_tolerance = float(
            self.get_parameter(
                'rack_final_marker_tolerance_m'
                if marker_id in (40, 41)
                else 'final_marker_tolerance_m'
            ).value
        )
        marker_forward_lower = desired_forward - marker_tolerance
        marker_forward_upper = desired_forward + marker_tolerance
        if marker_id == 41:
            marker_forward_upper = float(self.get_parameter(
                'marker_41_final_forward_max_m'
            ).value)
        now_ns = self.get_clock().now().nanoseconds
        marker_age = (
            (now_ns - self.last_detection_ns) / 1e9
            if self.last_detection_ns
            else math.inf
        )
        marker_fresh = marker_age <= float(
            self.get_parameter('detection_timeout_sec').value
        )
        if not marker_fresh or self.detection is None or not self.detection.visible:
            # 영상/검출이 순간 끊기면 전진은 즉시 멈추되, detector가
            # 복구될 시간을 준다. 유실 상태에서 계속 전진하지 않는다.
            self.velocity(0.0, 0.0)
            grace = float(
                self.get_parameter('final_insert_detection_grace_sec').value
            )
            if marker_age <= grace:
                return
            self.finish(
                'FAILED',
                '최종 직선 삽입 중 ArUco 마커 재검출 실패로 안전 정지 '
                f'(loss={marker_age:.1f}s)',
            )
            return

        lateral_error = (
            self.filtered_lateral - self.target(marker_id, 'lateral_m')
        )
        yaw_error = wrap_angle(
            self.filtered_yaw - self.target(marker_id, 'yaw_rad')
        )
        max_lateral = float(
            self.get_parameter(
                'marker_42_final_max_lateral_error_m'
                if self.marker_42_legacy_mode(marker_id)
                else 'final_insert_max_lateral_error_m'
            ).value
        )
        max_yaw = float(
            self.get_parameter('final_insert_max_yaw_error_rad').value
        )
        if abs(lateral_error) > max_lateral or abs(yaw_error) > max_yaw:
            self.finish(
                'FAILED',
                '최종 직선 삽입 중 정렬 이탈로 안전 정지 '
                f'(dl={lateral_error:+.3f}m, '
                f'dyaw={math.degrees(yaw_error):+.1f}deg)',
            )
            return

        wrong_direction_margin = float(
            self.get_parameter(
                'final_insert_wrong_direction_margin_m'
            ).value
        )
        if (
            self.final_insert_marker_forward_start is not None
            and self.filtered_forward
            > self.final_insert_marker_forward_start + wrong_direction_margin
        ):
            self.finish(
                'FAILED',
                '최종 삽입 중 마커 거리가 증가해 반대 방향 이동으로 판단 '
                f'({self.final_insert_marker_forward_start:.3f}'
                f'→{self.filtered_forward:.3f}m)',
            )
            return

        if elapsed > timeout:
            self.finish(
                'FAILED',
                '최종 삽입 제한시간 초과 '
                f'(odom={travelled:.3f}/{target:.3f}m, '
                f'marker_forward={self.filtered_forward:.3f}m)',
            )
            return

        final_pose_aligned = True
        if self.marker_42_legacy_mode(marker_id):
            final_pose_aligned = (
                abs(lateral_error)
                <= float(
                    self.get_parameter(
                        'marker_42_final_lateral_tolerance_m'
                    ).value
                )
                and abs(yaw_error)
                <= float(
                    self.get_parameter(
                        'marker_42_final_yaw_tolerance_rad'
                    ).value
                )
            )
        if (
            marker_fresh
            and self.detection is not None
            and self.detection.visible
            and self.filtered_forward is not None
            and abs(self.filtered_forward - desired_forward) <= marker_tolerance
            and final_pose_aligned
        ):
            self.final_marker_stable_count += 1
        else:
            self.final_marker_stable_count = 0

        marker_reached = self.final_marker_stable_count >= int(
            self.get_parameter('final_marker_stable_cycles').value
        )
        if marker_reached:
            marker_forward_text = (
                f'{self.filtered_forward:.3f}m'
                if self.filtered_forward is not None
                else 'unavailable'
            )
            self.finish(
                'SUCCEEDED',
                f'정밀 도킹 완료 (ArUco 판정, '
                f'odom={travelled:.3f}/{target:.3f}m, '
                f'marker_forward={marker_forward_text})',
            )
            return

        if travelled >= target:
            extension = (
                float(self.get_parameter(
                    'marker_42_final_insert_extension_m'
                ).value) if self.marker_42_legacy_mode(marker_id) else 0.0
            )
            marker_still_far = (
                self.filtered_forward > desired_forward + marker_tolerance
            )
            if (
                self.marker_42_legacy_mode(marker_id)
                and marker_still_far
                and final_pose_aligned
                and travelled < target + extension
            ):
                self.velocity(
                    float(self.get_parameter('final_insert_speed_mps').value),
                    0.0,
                )
                return
            # odom 상한에 도착하면서 처음 ArUco 허용 범위에 들어올 수 있다.
            # 모터를 정지하고 필요한 연속 프레임을 더 확인한다.
            if self.final_marker_stable_count > 0:
                self.velocity(0.0, 0.0)
                return
            self.finish(
                'FAILED',
                'odom 목표거리는 이동했지만 ArUco 최종 위치가 확인되지 않음 '
                f'(odom={travelled:.3f}/{target:.3f}m, '
                f'marker_forward={self.filtered_forward:.3f}m)',
            )
            return

        self.velocity(
            float(self.get_parameter('final_insert_speed_mps').value),
            0.0,
        )

    def control_home_recovery(
        self, now, marker_id, forward_error, lateral_error, yaw_error
    ):
        tight_yaw = float(self.get_parameter(
            'home_recovery_yaw_tolerance_rad'
        ).value)
        if self.dock_phase == 'HOME_RECOVERY_ALIGN':
            if abs(yaw_error) > tight_yaw:
                self.home_recovery_yaw_stable_count = 0
                maximum = float(self.get_parameter(
                    'home_max_angular_rps'
                ).value)
                minimum = float(self.get_parameter(
                    'axis_rotate_min_rps'
                ).value)
                angular = clamp(yaw_error, -maximum, maximum)
                if abs(angular) < minimum:
                    angular = math.copysign(minimum, angular)
                self.velocity(0.0, angular)
                return
            self.home_recovery_yaw_stable_count += 1
            self.velocity(0.0, 0.0)
            stable = int(self.get_parameter(
                'home_recovery_yaw_stable_cycles'
            ).value)
            if self.home_recovery_yaw_stable_count < stable:
                return
            if self.odom_pose is None:
                self.finish('FAILED', '/odom을 받지 못해 HOME 복구할 수 없습니다')
                return
            self.home_recovery_start = self.odom_pose
            self.home_recovery_started_ns = now
            self.home_recovery_yaw_stable_count = 0
            self.dock_phase = 'HOME_RECOVERY_BACKOFF'
            self.publish_state(
                self.command.task_id,
                marker_id,
                'RUNNING',
                'HOME 마커 정면 확인 완료: 0.050m 무회전 직선 후진 시작',
            )
            return

        if abs(yaw_error) > tight_yaw:
            self.dock_phase = 'HOME_RECOVERY_ALIGN'
            self.home_recovery_yaw_stable_count = 0
            self.velocity(0.0, 0.0)
            self.publish_state(
                self.command.task_id,
                marker_id,
                'RUNNING',
                'HOME 후진 중 방향 이탈 감지: 즉시 정지 후 정면 재정렬',
            )
            return
        if self.odom_pose is None or self.home_recovery_start is None:
            self.finish('FAILED', '/odom HOME 복구 기준을 잃었습니다')
            return
        elapsed = (now - self.home_recovery_started_ns) / 1e9
        start_x, start_y, _ = self.home_recovery_start
        travelled = math.hypot(
            self.odom_pose[0] - start_x, self.odom_pose[1] - start_y
        )
        target = float(self.get_parameter('home_recovery_backoff_m').value)
        if elapsed > float(self.get_parameter(
            'home_recovery_timeout_sec'
        ).value):
            self.finish(
                'FAILED',
                f'HOME 정면 직선 후진 제한시간 초과 ({travelled:.3f}/{target:.3f}m)',
            )
            return
        if travelled >= target:
            self.dock_phase = 'FINAL'
            self.home_recovery_start = None
            self.velocity(0.0, 0.0)
            self.publish_state(
                self.command.task_id,
                marker_id,
                'RUNNING',
                f'HOME {travelled:.3f}m 정면 후진 완료: 곡선 전진 재접근',
            )
            return
        self.velocity(
            -float(self.get_parameter('home_recovery_backoff_speed_mps').value),
            0.0,
        )

    def begin_home_recovery(self, forward_error, lateral_error, yaw_error):
        maximum = int(self.get_parameter('home_recovery_max_count').value)
        if self.home_recovery_count >= maximum:
            self.finish(
                'FAILED',
                'HOME 자동 복구 횟수 초과 '
                f'({self.home_recovery_count}/{maximum})',
            )
            return False
        self.home_recovery_count += 1
        self.home_recovery_start = None
        self.home_recovery_started_ns = 0
        self.home_recovery_yaw_stable_count = 0
        self.dock_phase = 'HOME_RECOVERY_ALIGN'
        self.velocity(0.0, 0.0)
        self.publish_state(
            self.command.task_id,
            int(self.command.marker_id),
            'RUNNING',
            'HOME 안전 복구: 정지 상태에서 마커 정면 정렬 시작 '
            f'({self.home_recovery_count}/{maximum}, '
            f'df={forward_error:+.3f}m, '
            f'dl={lateral_error:+.3f}m, '
            f'dyaw={math.degrees(yaw_error):+.1f}deg)',
        )
        return True

    def control_backoff(self):
        if self.odom_pose is None or self.backoff_start is None:
            self.finish('FAILED', '/odom 후진 기준을 잃었습니다')
            return
        now = self.get_clock().now().nanoseconds
        elapsed = (now - self.started_ns) / 1e9
        start_x, start_y, start_yaw = self.backoff_start
        current_x, current_y, current_yaw = self.odom_pose
        travelled = math.hypot(current_x - start_x, current_y - start_y)
        parameter = {
            'BACKOFF_RACK': 'backoff_rack_distance_m',
            'BACKOFF_DROP': 'backoff_drop_distance_m',
            'BACKOFF_HOME': 'backoff_home_distance_m',
        }[self.mode]
        target_distance = float(self.get_parameter(parameter).value)
        if elapsed > float(self.get_parameter('backoff_timeout_sec').value):
            self.finish(
                'FAILED',
                f'후진 이탈 제한시간 초과 '
                f'(odom={travelled:.3f}/{target_distance:.3f}m)',
            )
            return
        if travelled >= target_distance:
            self.finish('SUCCEEDED', f'{travelled:.3f}m 후진 이탈 완료')
            return
        self.velocity(
            -float(self.get_parameter('backoff_speed_mps').value), 0.0
        )

    def search_angular_velocity(self):
        if (
            not self.home_alignment
            and self.command is not None
            and int(self.command.marker_id) == 42
        ):
            return float(self.get_parameter(
                'marker_42_search_angular_rps'
            ).value)
        parameter = (
            'home_search_command_angular_rps'
            if self.home_alignment
            else 'search_command_angular_rps'
        )
        return float(self.get_parameter(parameter).value)

    def control_home_step_search(self, now):
        """Search HOME by fast turn, stationary look, then turn/look steps."""
        if self.odom_pose is None:
            self.finish('FAILED', '/odom을 받지 못해 HOME 마커를 탐색할 수 없습니다')
            return
        current_yaw = self.odom_pose[2]
        if self.home_search_turn_start_yaw is None:
            self.home_search_turn_start_yaw = current_yaw

        if self.home_search_phase == 'FAST_TURN':
            turned = abs(wrap_angle(
                current_yaw - self.home_search_turn_start_yaw
            ))
            target = float(self.get_parameter(
                'home_search_fast_turn_rad'
            ).value)
            if turned < target:
                self.velocity(0.0, self.search_angular_velocity())
                return
            self.home_search_phase = 'INITIAL_PAUSE'
            self.home_search_pause_started_ns = now
            self.velocity(0.0, 0.0)
            self.publish_state(
                self.command.task_id,
                int(self.command.marker_id),
                'RUNNING',
                'HOME 예상 방향 도착: 2초 정지 후 마커 41 확인',
            )
            return

        if self.home_search_phase in ('INITIAL_PAUSE', 'STEP_PAUSE'):
            pause_parameter = (
                'home_search_initial_pause_sec'
                if self.home_search_phase == 'INITIAL_PAUSE'
                else 'home_search_step_pause_sec'
            )
            pause = float(self.get_parameter(pause_parameter).value)
            elapsed = (now - self.home_search_pause_started_ns) / 1e9
            self.velocity(0.0, 0.0)
            if elapsed < pause:
                return
            self.home_search_phase = 'STEP_TURN'
            self.home_search_turn_start_yaw = current_yaw
            return

        turned = abs(wrap_angle(
            current_yaw - self.home_search_turn_start_yaw
        ))
        target = float(self.get_parameter('home_search_step_rad').value)
        if turned < target:
            self.velocity(
                0.0,
                float(self.get_parameter(
                    'home_search_step_angular_rps'
                ).value),
            )
            return
        self.home_search_phase = 'STEP_PAUSE'
        self.home_search_pause_started_ns = now
        self.velocity(0.0, 0.0)

    def home_visible_forward_safe(self):
        """Allow HOME forward motion only under fresh, bounded marker vision."""
        marker_forward = float(self.filtered_forward)
        maximum_marker_forward = float(self.get_parameter(
            'home_max_visible_marker_forward_m'
        ).value)
        if marker_forward > maximum_marker_forward:
            self.finish(
                'FAILED',
                'HOME 마커가 너무 멀어 전진하지 않습니다 '
                f'(forward={marker_forward:.3f}m)',
            )
            return False
        if self.odom_pose is None:
            self.finish('FAILED', '/odom을 받지 못해 HOME 전진할 수 없습니다')
            return False
        if self.home_forward_start_pose is None:
            self.home_forward_start_pose = self.odom_pose
        travelled = math.hypot(
            self.odom_pose[0] - self.home_forward_start_pose[0],
            self.odom_pose[1] - self.home_forward_start_pose[1],
        )
        maximum_travel = float(self.get_parameter(
            'home_max_visible_forward_travel_m'
        ).value)
        if travelled >= maximum_travel:
            self.finish(
                'FAILED',
                'HOME 마커 추종 최대 전진거리 도달로 안전 정지 '
                f'(odom={travelled:.3f}/{maximum_travel:.3f}m)',
            )
            return False
        return True

    def marker_42_legacy_mode(self, marker_id):
        return (
            int(marker_id) == 42
            and not bool(
                self.get_parameter('marker_42_use_rack_docking').value
            )
        )

    def staging_side_move_enabled(self, marker_id):
        return int(marker_id) in {
            int(value)
            for value in self.get_parameter(
                'staging_side_move_marker_ids'
            ).value
        }

    def target(self, marker_id, suffix):
        if self.home_alignment and not suffix.startswith('staging_'):
            parameter = f'home_marker_{marker_id}_target_{suffix}'
            return float(self.get_parameter(parameter).value)
        parameter = (
            f'marker_{marker_id}_{suffix}'
            if suffix.startswith('staging_')
            else f'marker_{marker_id}_target_{suffix}'
        )
        return float(
            self.get_parameter(parameter).value
        )

    def velocity(self, linear, angular):
        msg = TwistStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        msg.twist.linear.x = float(linear)
        msg.twist.angular.z = float(angular)
        self.cmd_pub.publish(msg)

    def finish(self, state, message):
        if self.command is None:
            return
        command = self.command
        self.velocity(0.0, 0.0)
        self.command = None
        self.mode = None
        self.detection = None
        self.backoff_start = None
        self.final_insert_start = None
        self.home_alignment = False
        self.home_fast_alignment = False
        self.home_forward_start_pose = None
        self.home_reverse_distance = 0.0
        self.home_reverse_last_pose = None
        self.home_reverse_announced = False
        self.home_search_acquired = False
        self.search_enabled = False
        self.marker_42_direct_start = False
        self.publish_state(command.task_id, command.marker_id, state, message)

    def publish_state(self, task_id, marker_id, state, message):
        self.state_pub.publish(
            DockState(
                task_id=task_id,
                marker_id=marker_id,
                state=state,
                message=message,
            )
        )

    def destroy_node(self):
        if rclpy.ok():
            self.velocity(0.0, 0.0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = ArucoDocking()
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
