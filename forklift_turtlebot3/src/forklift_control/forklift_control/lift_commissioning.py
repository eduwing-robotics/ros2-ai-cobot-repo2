#!/usr/bin/env python3
"""Explicitly armed HEIGHT_2-HEIGHT_3-PARK commissioning sequence."""

import time

import rclpy
from rclpy.node import Node

from forklift_interfaces.msg import LiftCommand, LiftState


class LiftCommissioning(Node):
    def __init__(self):
        super().__init__('lift_commissioning')
        self.declare_parameter('armed', False)
        self.declare_parameter('sequence', ['HEIGHT_2', 'HEIGHT_3', 'PARK'])
        self.declare_parameter('step_timeout_sec', 90.0)
        self.declare_parameter('dwell_sec', 2.0)
        self.publisher = self.create_publisher(
            LiftCommand, '/forklift/lift/command', 10
        )
        self.create_subscription(
            LiftState, '/forklift/lift/state', self.on_state, 10
        )
        self.commands = [
            str(value).strip().upper()
            for value in self.get_parameter('sequence').value
        ]
        self.index = -1
        self.expected = ''
        self.deadline = 0.0
        self.next_send_at = 0.0
        self.done = False
        self.success = False
        self.create_timer(1.0, self.start_once)

    def start_once(self):
        if self.index != -1:
            return
        if not bool(self.get_parameter('armed').value):
            self.get_logger().warning(
                '잠금 상태입니다. 실제 시험은 포크 주변을 비우고 ZERO 후 armed:=true로 실행하세요'
            )
            self.done = True
            return
        if not self.commands or any(
            value not in ('HEIGHT_2', 'HEIGHT_3', 'PARK')
            for value in self.commands
        ):
            self.get_logger().error(
                'sequence에는 HEIGHT_2, HEIGHT_3, PARK만 사용할 수 있습니다'
            )
            self.done = True
            return
        self.get_logger().warning(
            '리프트 단계 시험 시작: 포크가 움직입니다. 비상 시 STOP을 보내세요'
        )
        self.index = 0
        self.send_current()

    def send_current(self):
        command = self.commands[self.index]
        self.expected = (
            'PARKED' if command == 'PARK' else f'AT_{command}'
        )
        self.deadline = time.monotonic() + float(
            self.get_parameter('step_timeout_sec').value
        )
        self.get_logger().info(
            f'[{self.index + 1}/{len(self.commands)}] {command} 이동 요청'
        )
        self.publisher.publish(
            LiftCommand(task_id='lift-commissioning', command=command)
        )

    def on_state(self, msg):
        if self.done or self.index < 0 or msg.task_id != 'lift-commissioning':
            return
        state = msg.state.strip().upper()
        if state == 'ERROR':
            self.get_logger().error(f'시험 중단: {msg.message}')
            self.done = True
            return
        if state != self.expected:
            return
        self.get_logger().info(f'{self.commands[self.index]} 도착 확인')
        self.expected = ''
        self.index += 1
        if self.index >= len(self.commands):
            self.get_logger().info('HEIGHT_2·HEIGHT_3 및 PARK 단계 시험 완료')
            self.success = True
            self.done = True
            return
        dwell = float(self.get_parameter('dwell_sec').value)
        self.next_send_at = time.monotonic() + dwell
        self.get_logger().info(f'{dwell:.1f}초 정지 후 다음 단계 진행')

    def check_timeout(self):
        if self.done or self.index < 0:
            return
        if self.next_send_at:
            if time.monotonic() >= self.next_send_at:
                self.next_send_at = 0.0
                self.send_current()
            return
        if not self.expected:
            return
        if time.monotonic() > self.deadline:
            self.publisher.publish(
                LiftCommand(task_id='lift-commissioning', command='STOP')
            )
            self.get_logger().error(f'{self.commands[self.index]} 이동 시간 초과')
            self.done = True


def main(args=None):
    rclpy.init(args=args)
    node = LiftCommissioning()
    try:
        while rclpy.ok() and not node.done:
            rclpy.spin_once(node, timeout_sec=0.1)
            node.check_timeout()
    except KeyboardInterrupt:
        node.publisher.publish(
            LiftCommand(task_id='lift-commissioning', command='STOP')
        )
    finally:
        success = node.success
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0 if success else 2


if __name__ == '__main__':
    raise SystemExit(main())
