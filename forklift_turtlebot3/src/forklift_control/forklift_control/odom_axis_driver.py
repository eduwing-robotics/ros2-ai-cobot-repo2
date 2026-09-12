#!/usr/bin/env python3
"""Map-free axis driving: rotate in place, drive straight, then rotate."""
import math
import rclpy
from geometry_msgs.msg import PoseStamped, TwistStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from forklift_interfaces.msg import DockState, ForkliftControlCommand, ForkliftTaskStatus


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def yaw_of(q):
    return math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))


class OdomAxisDriver(Node):
    def __init__(self):
        super().__init__('odom_axis_driver')
        params = (
            ('point_a_pose', [0.26804, 0.02592, 0.01007]),
            ('point_a_marker_id', 41),
            ('point_b_pose', [0.314, -0.201, -0.018]),
            ('rack2_lane_pose', [0.0, -0.185, 0.0]),
            ('rack2_lane_clearance_forward_m', 0.03),
            ('home_behind_pose', [-0.2350, 0.0087, 3.112]),
            ('drop_pose', [-0.205, -0.330, -1.460]),
            ('drop_marker_id', 42),
            ('linear_speed_mps', 0.04), ('angular_speed_rps', 0.25),
            ('angular_kp', 1.5), ('min_angular_rps', 0.08),
            ('distance_tolerance_m', 0.015), ('yaw_tolerance_rad', 0.025),
            ('axis_skip_distance_m', 0.025), ('front_stop_distance_m', 0.25),
            ('front_sector_rad', 0.35), ('scan_timeout_sec', 2.0),
            ('scan_recovery_timeout_sec', 30.0),
            ('pose_topic', '/forklift/mobile_robot_pose'),
            ('pose_frame_id', 'map'),
            ('pose_publish_hz', 10.0),
            ('landmark_reanchor_enabled', False),
        )
        for name, default in params:
            self.declare_parameter(name, default)
        self.pose = self.home = None
        self.front = math.inf
        self.scan_ns = 0
        self.scan_lost_started_ns = None
        self.scan_wait_announced = False
        self.phase = 'IDLE'
        self.command_id = self.goal_name = ''
        self.parts = []
        self.part = 0
        self.start_xy = None
        self.last_reanchor_task_id = None
        self.cmd_pub = self.create_publisher(TwistStamped, '/cmd_vel', 10)
        self.state_pub = self.create_publisher(ForkliftTaskStatus, '/forklift/route/state', 10)
        self.pose_pub = self.create_publisher(
            PoseStamped, str(self.p('pose_topic')), 10
        )
        self.create_subscription(ForkliftControlCommand, '/forklift/route/command', self.on_command, 10)
        self.create_subscription(Odometry, '/odom', self.on_odom, qos_profile_sensor_data)
        self.create_subscription(LaserScan, '/scan', self.on_scan, qos_profile_sensor_data)
        self.create_subscription(DockState, '/forklift/dock/state', self.on_dock_state, 10)
        self.create_timer(0.05, self.control)
        self.create_timer(
            1.0 / max(1.0, float(self.p('pose_publish_hz'))),
            self.publish_logical_pose,
        )
        self.get_logger().info('첫 /odom을 고정 HOME 기준으로 저장합니다')

    def p(self, name):
        return self.get_parameter(name).value

    def on_odom(self, msg):
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        self.pose = (float(p.x), float(p.y), yaw_of(q))
        if self.home is None:
            self.home = self.pose
            self.get_logger().info('HOME 기준 저장 완료')

    def on_scan(self, msg):
        self.front = math.inf
        for i, value in enumerate(msg.ranges):
            angle = wrap(msg.angle_min + i*msg.angle_increment)
            if abs(angle) <= float(self.p('front_sector_rad')) and math.isfinite(value):
                if msg.range_min < value < msg.range_max:
                    self.front = min(self.front, float(value))
        self.scan_ns = self.get_clock().now().nanoseconds

    def logical_pose(self):
        x, y, a = self.pose
        hx, hy, ha = self.home
        dx, dy = x-hx, y-hy
        return (math.cos(ha)*dx+math.sin(ha)*dy,
                -math.sin(ha)*dx+math.cos(ha)*dy, wrap(a-ha))

    def publish_logical_pose(self):
        if self.pose is None or self.home is None:
            return
        x, y, yaw = self.logical_pose()
        msg = PoseStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = str(self.p('pose_frame_id'))
        msg.pose.position.x = float(x)
        msg.pose.position.y = float(y)
        msg.pose.position.z = 0.0
        msg.pose.orientation.z = math.sin(yaw / 2.0)
        msg.pose.orientation.w = math.cos(yaw / 2.0)
        self.pose_pub.publish(msg)

    def set_logical_anchor(self, logical_pose):
        """Shift the HOME transform so the current odom equals a known landmark."""
        x, y, raw_yaw = self.pose
        gx, gy, goal_yaw = map(float, logical_pose)
        home_yaw = wrap(raw_yaw - goal_yaw)
        c, s = math.cos(home_yaw), math.sin(home_yaw)
        home_x = x - (c * gx - s * gy)
        home_y = y - (s * gx + c * gy)
        self.home = (home_x, home_y, home_yaw)

    def on_dock_state(self, msg):
        if (
            self.pose is None
            or self.phase != 'IDLE'
            or msg.state.strip().upper() != 'SUCCEEDED'
            or msg.task_id == self.last_reanchor_task_id
        ):
            return
        if 'HOME ArUco 정렬 완료' in msg.message:
            self.set_logical_anchor([0.0, 0.0, 0.0])
            self.last_reanchor_task_id = msg.task_id
            self.get_logger().info(
                'HOME ArUco 기준으로 odom 좌표를 (0,0,0)에 재정렬했습니다'
            )
            return
        if (
            not bool(self.p('landmark_reanchor_enabled'))
            or '후진 이탈 완료' not in msg.message
        ):
            return
        anchors = {
            41: ('POINT_A_MARKER_41', list(self.p('point_a_pose'))),
            40: ('POINT_B_MARKER_40', list(self.p('point_b_pose'))),
            42: ('DROP_MARKER_42', list(self.p('drop_pose'))),
        }
        anchor = anchors.get(int(msg.marker_id))
        if anchor is None:
            return
        name, logical_pose = anchor
        self.set_logical_anchor(logical_pose)
        self.last_reanchor_task_id = msg.task_id
        self.get_logger().info(
            f'{name}: 도킹 후진 완료 위치로 odom 누적오차 보정 완료'
        )

    def on_command(self, msg):
        command = msg.command.strip().upper()
        if command == 'STOP':
            self.finish('STOPPED', '사용자 정지')
            return
        if command in ('GET_POSE', 'REPORT_POSE'):
            if self.pose is None or self.home is None:
                self.publish(msg.command_id, 'FAILED', '/odom 또는 HOME 기준이 없습니다')
            else:
                x, y, yaw = self.logical_pose()
                self.publish(
                    msg.command_id,
                    'SUCCEEDED',
                    f'logical_pose=[{x:.5f}, {y:.5f}, {yaw:.5f}]',
                )
            return
        if command == 'SET_HOME':
            if self.phase != 'IDLE' or self.pose is None:
                self.publish(msg.command_id, 'FAILED', '정지 상태의 /odom이 필요합니다')
            else:
                self.home = self.pose
                self.last_reanchor_task_id = None
                self.publish(msg.command_id, 'SUCCEEDED', '현재 자세를 HOME으로 저장')
            return
        if self.phase != 'IDLE':
            self.publish(msg.command_id, 'FAILED', '이미 주행 중입니다')
            return
        goals = {
            'GO_HOME': ('HOME', [0.0, 0.0, 0.0]),
            'GO_HOME_FRONT': ('HOME_FRONT', [0.0, 0.0, 0.0]),
            'GO_HOME_BEHIND': ('HOME_BEHIND', list(self.p('home_behind_pose'))),
            'GO_RACK2_LANE': (
                'RACK2_LANE_MARKER_40', list(self.p('rack2_lane_pose'))
            ),
            'GO_POINT_A': ('POINT_A_MARKER_41', list(self.p('point_a_pose'))),
            'GO_POINT_B': ('POINT_B_MARKER_40', list(self.p('point_b_pose'))),
            'GO_DROP': ('DROP_MARKER_42', list(self.p('drop_pose'))),
        }
        if command not in goals or self.home is None:
            self.publish(msg.command_id, 'FAILED', '잘못된 명령 또는 HOME 없음')
            return
        self.command_id = msg.command_id
        self.goal_name, goal = goals[command]
        self.scan_lost_started_ns = None
        self.scan_wait_announced = False
        cx, cy, current_yaw = self.logical_pose()
        gx, gy, ga = map(float, goal)
        skip = float(self.p('axis_skip_distance_m'))
        self.parts = []
        if command == 'GO_RACK2_LANE':
            clearance = float(self.p('rack2_lane_clearance_forward_m'))
            if clearance > 0.0:
                # STRAIGHT completion subtracts distance_tolerance_m. Add it
                # here so the physical clearance motion remains a full 3 cm.
                clearance += float(self.p('distance_tolerance_m'))
                self.parts.append((clearance, current_yaw, 'CLEARANCE'))
        if abs(gx-cx) >= skip:
            self.parts.append((abs(gx-cx), 0.0 if gx > cx else math.pi, 'X'))
        if abs(gy-cy) >= skip:
            self.parts.append((abs(gy-cy), math.pi/2 if gy > cy else -math.pi/2, 'Y'))
        # HOME 경유 시 마지막 구간을 X축으로 만들어 통로 방향으로 도착
        if command in ('GO_HOME', 'GO_HOME_FRONT') and len(self.parts) >= 2:
            self.parts = [self.parts[1], self.parts[0]]
        # HOME에서는 마지막 직진 방향을 유지해 도착 후 회전하지 않는다.
        if command in ('GO_HOME', 'GO_HOME_BEHIND') and self.parts:
            ga = self.parts[-1][1]
        self.parts.append((0.0, ga, 'FINAL'))
        self.part, self.start_xy, self.phase = 0, None, 'ROTATE'
        self.publish(self.command_id, 'RUNNING', f'{self.goal_name}: 회전→직진 방식 시작')

    def control(self):
        if self.phase == 'IDLE' or self.pose is None:
            return
        distance, heading, kind = self.parts[self.part]
        _, _, current_yaw = self.logical_pose()
        if self.phase == 'ROTATE':
            error = wrap(heading-current_yaw)
            if abs(error) <= float(self.p('yaw_tolerance_rad')):
                self.stop()
                if kind == 'FINAL':
                    self.finish('SUCCEEDED', f'{self.goal_name} odom 이동 완료')
                else:
                    self.start_xy = self.logical_pose()[:2]
                    self.phase = 'STRAIGHT'
                return
            speed = min(float(self.p('angular_speed_rps')),
                        max(float(self.p('min_angular_rps')), float(self.p('angular_kp'))*abs(error)))
            self.velocity(0.0, math.copysign(speed, error))  # 제자리 회전만
            return
        now_ns = self.get_clock().now().nanoseconds
        age = (now_ns-self.scan_ns)/1e9 if self.scan_ns else math.inf
        if age > float(self.p('scan_timeout_sec')):
            self.stop()
            if self.scan_lost_started_ns is None:
                self.scan_lost_started_ns = now_ns
            wait_time = (now_ns-self.scan_lost_started_ns)/1e9
            if not self.scan_wait_announced:
                self.publish(
                    self.command_id,
                    'RUNNING',
                    '/scan 일시 중단: 정지 상태로 복구 대기',
                )
                self.scan_wait_announced = True
            if wait_time > float(self.p('scan_recovery_timeout_sec')):
                self.finish(
                    'FAILED',
                    '/scan 복구 제한시간 초과로 안전 정지',
                )
            return
        if self.scan_lost_started_ns is not None:
            self.scan_lost_started_ns = None
            self.scan_wait_announced = False
            self.publish(
                self.command_id,
                'RUNNING',
                '/scan 복구 완료: 기존 경로 주행 재개',
            )
        if self.front < float(self.p('front_stop_distance_m')):
            self.finish('FAILED', f'전방 장애물 {self.front:.3f}m 안전 정지')
            return
        x, y, _ = self.logical_pose()
        sx, sy = self.start_xy
        travelled = math.hypot(x-sx, y-sy)
        if travelled >= distance-float(self.p('distance_tolerance_m')):
            self.stop()
            self.part += 1
            self.phase = 'ROTATE'
            return
        self.velocity(float(self.p('linear_speed_mps')), 0.0)  # 직선만

    def velocity(self, linear, angular):
        msg = TwistStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        msg.twist.linear.x, msg.twist.angular.z = float(linear), float(angular)
        self.cmd_pub.publish(msg)

    def stop(self):
        self.velocity(0.0, 0.0)

    def finish(self, state, text):
        cid, marker = self.command_id, 41 if self.goal_name == 'POINT_A_MARKER_41' else 0
        self.stop()
        self.phase, self.command_id, self.goal_name = 'IDLE', '', ''
        self.parts, self.start_xy = [], None
        self.publish(cid, state, text, marker)

    def publish(self, cid, state, text, marker=0):
        self.state_pub.publish(ForkliftTaskStatus(
            task_id=cid, command_id=cid, state=state,
            progress=1.0 if state == 'SUCCEEDED' else 0.0,
            success=state == 'SUCCEEDED', message=text,
            current_rack_marker_id=marker, cycle_index=0, cycle_total=0))

    def destroy_node(self):
        if rclpy.ok():
            self.stop()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = OdomAxisDriver()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
