#!/usr/bin/env python3
"""Safely arbitrate automatic and Unity manual velocity commands."""

import math
import time

import rclpy
from geometry_msgs.msg import TwistStamped
from rclpy.node import Node

from forklift_interfaces.msg import (
    DockCommand,
    ForkliftControlCommand,
    ForkliftTaskStatus,
    LiftCommand,
)


class CmdVelArbiter(Node):
    """Make this node the only publisher to the physical ``/cmd_vel``."""

    def __init__(self):
        super().__init__('cmd_vel_arbiter')
        defaults = (
            ('manual_timeout_sec', 0.30),
            ('auto_timeout_sec', 0.50),
            ('output_period_sec', 0.05),
            ('state_heartbeat_sec', 1.0),
            ('max_forward_mps', 0.08),
            ('max_reverse_mps', 0.05),
            ('max_angular_rps', 0.35),
        )
        for name, value in defaults:
            self.declare_parameter(name, value)

        self.manual_timeout = float(
            self.get_parameter('manual_timeout_sec').value
        )
        self.auto_timeout = float(self.get_parameter('auto_timeout_sec').value)
        self.max_forward = float(self.get_parameter('max_forward_mps').value)
        self.max_reverse = float(self.get_parameter('max_reverse_mps').value)
        self.max_angular = float(self.get_parameter('max_angular_rps').value)

        self.output_pub = self.create_publisher(TwistStamped, '/cmd_vel', 10)
        self.state_pub = self.create_publisher(
            ForkliftTaskStatus, '/forklift/manual/state', 10
        )
        self.route_stop_pub = self.create_publisher(
            ForkliftControlCommand, '/forklift/route/command', 10
        )
        self.dock_stop_pub = self.create_publisher(
            DockCommand, '/forklift/dock/command', 10
        )
        self.lift_stop_pub = self.create_publisher(
            LiftCommand, '/forklift/lift/command', 10
        )

        self.create_subscription(
            TwistStamped, '/forklift/cmd_vel/route', self.on_route_velocity, 10
        )
        self.create_subscription(
            TwistStamped, '/forklift/cmd_vel/dock', self.on_dock_velocity, 10
        )
        self.create_subscription(
            TwistStamped, '/forklift/manual/cmd_vel', self.on_manual_velocity, 10
        )
        self.create_subscription(
            ForkliftControlCommand,
            '/forklift/manual/control',
            self.on_manual_control,
            10,
        )

        self.manual_mode = False
        self.session_id = ''
        self.manual_velocity = self.zero_message()
        self.last_manual_at = None
        self.manual_timed_out = False
        self.last_auto_at = None
        self.auto_watchdog_stopped = True
        self.state = 'AUTO'
        self.state_message = '자동 주행 모드'
        self.last_state_publish = 0.0

        self.create_timer(
            float(self.get_parameter('output_period_sec').value), self.update
        )
        self.publish_state('AUTO', '자동 주행 모드')

    def zero_message(self):
        msg = TwistStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        return msg

    def publish_zero(self):
        self.output_pub.publish(self.zero_message())

    def forward_auto(self, msg):
        if self.manual_mode:
            return
        self.last_auto_at = time.monotonic()
        self.auto_watchdog_stopped = False
        self.output_pub.publish(msg)

    def on_route_velocity(self, msg):
        self.forward_auto(msg)

    def on_dock_velocity(self, msg):
        self.forward_auto(msg)

    def on_manual_control(self, msg):
        command = msg.command.strip().upper()
        command_id = msg.command_id.strip()
        if command == 'TAKE_CONTROL':
            self.take_control(command_id)
        elif command == 'STOP':
            self.emergency_stop(command_id)
        elif command == 'RELEASE_CONTROL':
            self.release_control(command_id)
        else:
            self.publish_state(
                'REJECTED',
                f'지원하지 않는 수동 제어 명령: {msg.command}',
                command_id=command_id,
            )

    def take_control(self, command_id):
        if not command_id:
            self.publish_state('REJECTED', 'command_id가 필요합니다')
            return
        if self.manual_mode and self.session_id != command_id:
            self.publish_state(
                'REJECTED',
                f'다른 수동 세션이 사용 중입니다: {self.session_id}',
                command_id=command_id,
            )
            return
        self.manual_mode = True
        self.session_id = command_id
        self.manual_velocity = self.zero_message()
        self.last_manual_at = time.monotonic()
        self.manual_timed_out = False
        self.publish_zero()
        self.stop_automatic_devices('manual-takeover')
        self.publish_state('MANUAL_READY', 'Unity 수동조작 준비 완료')

    def emergency_stop(self, command_id):
        if not self.manual_mode:
            self.manual_mode = True
            self.session_id = command_id or 'emergency-stop'
        self.manual_velocity = self.zero_message()
        self.last_manual_at = None
        self.manual_timed_out = True
        self.publish_zero()
        self.stop_automatic_devices('manual-stop')
        self.publish_state('STOPPED', '수동 STOP이 적용되었습니다')

    def release_control(self, command_id):
        if not self.manual_mode:
            self.publish_zero()
            self.publish_state('AUTO', '이미 자동 주행 모드입니다')
            return
        if command_id and command_id != self.session_id:
            self.publish_state(
                'REJECTED',
                '현재 수동 세션과 command_id가 다릅니다',
                command_id=command_id,
            )
            return
        self.publish_zero()
        self.manual_mode = False
        self.session_id = ''
        self.last_manual_at = None
        self.manual_timed_out = False
        self.auto_watchdog_stopped = True
        self.publish_state('AUTO', '수동조작 종료; 자동 작업은 재개되지 않습니다')

    def on_manual_velocity(self, msg):
        if not self.manual_mode or self.manual_timed_out:
            return
        # frame_id를 수동 세션 ID로 사용해 거절된/오래된 클라이언트의
        # 속도 패킷이 현재 세션에 섞이지 않게 합니다.
        if msg.header.frame_id != self.session_id:
            return
        linear = float(msg.twist.linear.x)
        angular = float(msg.twist.angular.z)
        if not math.isfinite(linear) or not math.isfinite(angular):
            self.emergency_stop(self.session_id)
            return
        safe = self.zero_message()
        safe.twist.linear.x = max(
            -self.max_reverse, min(self.max_forward, linear)
        )
        safe.twist.angular.z = max(
            -self.max_angular, min(self.max_angular, angular)
        )
        self.manual_velocity = safe
        self.last_manual_at = time.monotonic()
        if self.state != 'MANUAL_ACTIVE':
            self.publish_state('MANUAL_ACTIVE', 'Unity 수동 속도 명령 수신 중')

    def stop_automatic_devices(self, reason):
        command_id = f'{reason}-{int(time.monotonic() * 1000)}'
        self.route_stop_pub.publish(
            ForkliftControlCommand(command_id=command_id, command='STOP')
        )
        self.dock_stop_pub.publish(
            DockCommand(task_id=command_id, marker_id=0, command='STOP')
        )
        self.lift_stop_pub.publish(
            LiftCommand(task_id=command_id, command='STOP')
        )

    def update(self):
        now = time.monotonic()
        if self.manual_mode:
            if (
                self.last_manual_at is None
                or now - self.last_manual_at > self.manual_timeout
            ):
                self.publish_zero()
                if not self.manual_timed_out:
                    self.manual_timed_out = True
                    self.publish_state(
                        'TIMED_OUT',
                        f'수동 명령이 {self.manual_timeout:.2f}초 동안 없어 정지',
                    )
            elif not self.manual_timed_out:
                self.manual_velocity.header.stamp = self.get_clock().now().to_msg()
                self.output_pub.publish(self.manual_velocity)
        elif (
            not self.auto_watchdog_stopped
            and self.last_auto_at is not None
            and now - self.last_auto_at > self.auto_timeout
        ):
            self.publish_zero()
            self.auto_watchdog_stopped = True

        heartbeat = float(self.get_parameter('state_heartbeat_sec').value)
        if now - self.last_state_publish >= heartbeat:
            self.publish_state(self.state, self.state_message)

    def publish_state(self, state, message, command_id=None):
        self.state = state
        self.state_message = message
        self.last_state_publish = time.monotonic()
        cid = self.session_id if command_id is None else command_id
        self.state_pub.publish(ForkliftTaskStatus(
            task_id='manual-drive',
            command_id=cid,
            state=state,
            progress=0.0,
            success=state not in ('REJECTED', 'TIMED_OUT'),
            message=message,
            current_rack_marker_id=0,
            cycle_index=0,
            cycle_total=0,
        ))
        log = (
            self.get_logger().error
            if state == 'REJECTED'
            else self.get_logger().info
        )
        log(f'{state}: {message}')

    def destroy_node(self):
        if rclpy.ok():
            self.publish_zero()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelArbiter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        except KeyboardInterrupt:
            pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
