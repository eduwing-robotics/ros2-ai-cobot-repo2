#!/usr/bin/env python3
import rclpy
from rclpy.node import Node

from forklift_interfaces.msg import DockCommand, DockState


class MockDocking(Node):
    def __init__(self):
        super().__init__('mock_aruco_docking')
        self.declare_parameter('delay_sec', 2.0)
        self.state_pub = self.create_publisher(DockState, '/forklift/dock/state', 10)
        self.create_subscription(
            DockCommand, '/forklift/dock/command', self.command_callback, 10
        )
        self.pending = []

    def command_callback(self, msg):
        command = msg.command.strip().upper()
        if command == 'STOP':
            return
        if command not in (
            'START', 'START_SEARCH', 'FINAL_ONLY',
            'ALIGN_HOME', 'ALIGN_HOME_SEARCH',
            'ALIGN_HOME_FAST', 'ALIGN_HOME_FAST_SEARCH',
            'BACKOFF_RACK', 'BACKOFF_DROP', 'BACKOFF_HOME',
        ):
            return
        self.get_logger().info(
            f'[MOCK] marker {msg.marker_id} command={command} 시작'
        )
        timer = self.create_timer(
            float(self.get_parameter('delay_sec').value),
            lambda: self.finish(msg.task_id, msg.marker_id, timer),
        )
        self.pending.append(timer)

    def finish(self, task_id, marker_id, timer):
        timer.cancel()
        self.destroy_timer(timer)
        if timer in self.pending:
            self.pending.remove(timer)
        msg = DockState()
        msg.task_id = task_id
        msg.marker_id = marker_id
        msg.state = 'SUCCEEDED'
        msg.message = '모의 도킹/후진 완료'
        self.state_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = MockDocking()
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
