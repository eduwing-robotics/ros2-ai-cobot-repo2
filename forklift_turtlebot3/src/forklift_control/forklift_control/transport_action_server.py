#!/usr/bin/env python3
"""Logical-location transport actions using odometry routes and ArUco docking."""

import re
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from forklift_interfaces.action import ExecuteTransport, ReturnHome
from forklift_interfaces.msg import (
    DockCommand,
    DockState,
    ForkliftControlCommand,
    ForkliftTaskStatus,
    LiftCommand,
    LiftState,
)


class TransportActionServer(Node):
    """Run one pickup/drop-off job without Nav2 or map coordinates."""

    LOCATION_CODES = ('HOME', 'HOME_BEHIND', 'RACK1', 'RACK2', 'DROP')
    TRANSPORT_CODES = ('RACK1', 'RACK2', 'DROP')
    CODE_ALIASES = {
        'RACK_1': 'RACK1',
        'RACK_2': 'RACK2',
        'POINT_A': 'RACK1',
        'POINT_B': 'RACK2',
        'DROP42': 'DROP',
        'HOME_FRONT': 'HOME',
    }

    def __init__(self):
        super().__init__('transport_action_server')
        defaults = (
            ('use_mock_route', False),
            ('mock_route_delay_sec', 0.25),
            ('use_mock_lift', False),
            ('mock_lift_delay_sec', 0.25),
            ('route_timeout_sec', 300.0),
            ('device_timeout_sec', 720.0),
            ('endpoint_wait_sec', 10.0),
            ('initial_location_code', 'HOME'),
            ('return_home_final_backoff_enabled', True),
            ('rack1_marker_id', 41),
            ('rack2_marker_id', 40),
            ('drop_marker_id', 42),
        )
        for name, value in defaults:
            self.declare_parameter(name, value)

        initial = self.normalize_code(
            str(self.get_parameter('initial_location_code').value)
        )
        self.current_code = initial if initial in self.LOCATION_CODES else 'HOME'
        self.manual_mode = False

        self.group = ReentrantCallbackGroup()
        self.route_pub = self.create_publisher(
            ForkliftControlCommand, '/forklift/route/command', 10
        )
        self.dock_pub = self.create_publisher(
            DockCommand, '/forklift/dock/command', 10
        )
        self.lift_pub = self.create_publisher(
            LiftCommand, '/forklift/lift/command', 10
        )
        self.create_subscription(
            ForkliftTaskStatus,
            '/forklift/route/state',
            self.on_route_state,
            10,
            callback_group=self.group,
        )
        self.create_subscription(
            DockState,
            '/forklift/dock/state',
            self.on_dock_state,
            10,
            callback_group=self.group,
        )
        self.create_subscription(
            LiftState,
            '/forklift/lift/state',
            self.on_lift_state,
            10,
            callback_group=self.group,
        )

        self.create_subscription(
            ForkliftTaskStatus,
            '/forklift/manual/state',
            self.on_manual_state,
            10,
            callback_group=self.group,
        )

        self.state_lock = threading.Lock()
        self.active = False
        self.task_id = ''
        self.operation_sequence = 0
        self.route_operation_id = ''
        self.dock_operation_id = ''
        self.lift_operation_id = ''
        self.route_event = threading.Event()
        self.dock_event = threading.Event()
        self.lift_event = threading.Event()
        self.route_result = None
        self.dock_result = None
        self.lift_result = None

        self.execute_server = ActionServer(
            self,
            ExecuteTransport,
            '/forklift/execute_transport',
            execute_callback=self.execute_transport,
            goal_callback=self.on_goal,
            cancel_callback=self.on_cancel,
            callback_group=self.group,
        )
        self.home_server = ActionServer(
            self,
            ReturnHome,
            '/forklift/return_home',
            execute_callback=self.return_home,
            goal_callback=self.on_goal,
            cancel_callback=self.on_cancel,
            callback_group=self.group,
        )
        lift_mock = bool(self.get_parameter('use_mock_lift').value)
        self.get_logger().info(
            'Logical transport ActionServer ready: '
            f'current_location={self.current_code}, lift_mock={lift_mock}'
        )

    def on_goal(self, _request):
        with self.state_lock:
            if self.active or self.manual_mode:
                return GoalResponse.REJECT
            self.active = True
        return GoalResponse.ACCEPT

    def on_manual_state(self, msg):
        if msg.state in (
            'TAKING_CONTROL',
            'MANUAL_READY',
            'MANUAL_ACTIVE',
            'STOPPED',
            'TIMED_OUT',
        ):
            self.manual_mode = True
        elif msg.state in ('AUTO', 'RELEASED'):
            self.manual_mode = False

    def on_cancel(self, _goal_handle):
        self.stop_devices()
        return CancelResponse.ACCEPT

    @classmethod
    def normalize_code(cls, value):
        code = str(value).strip().upper().replace('-', '_').replace(' ', '_')
        return cls.CODE_ALIASES.get(code, code)

    def marker_id(self, code):
        names = {
            'RACK1': 'rack1_marker_id',
            'RACK2': 'rack2_marker_id',
            'DROP': 'drop_marker_id',
        }
        return int(self.get_parameter(names[code]).value)

    @staticmethod
    def backoff_command(code):
        return 'BACKOFF_DROP' if code == 'DROP' else 'BACKOFF_RACK'

    @staticmethod
    def insertion_height(code):
        """Return the lift command/state required to enter a location."""
        return 'HEIGHT_2', 'AT_HEIGHT_2', 'height 2 (RACK/DROP)'

    def execute_transport(self, goal_handle):
        goal = goal_handle.request
        result = ExecuteTransport.Result()
        self.task_id = goal.req_id.strip() or f'delivery-{goal.delivery_id}'
        try:
            if not goal.req_id.strip():
                return self.abort(
                    goal_handle, result, 'INVALID_GOAL', 'req_id is required'
                )
            pickup = self.normalize_code(goal.pickup_code)
            dropoff = self.normalize_code(goal.dropoff_code)
            if pickup not in self.TRANSPORT_CODES:
                return self.abort(
                    goal_handle,
                    result,
                    'INVALID_PICKUP_CODE',
                    f'unsupported pickup_code: {goal.pickup_code}',
                )
            if dropoff not in self.TRANSPORT_CODES:
                return self.abort(
                    goal_handle,
                    result,
                    'INVALID_DROPOFF_CODE',
                    f'unsupported dropoff_code: {goal.dropoff_code}',
                )
            if pickup == dropoff:
                return self.abort(
                    goal_handle,
                    result,
                    'SAME_LOCATION',
                    'pickup_code and dropoff_code must be different',
                )

            # Video scenario 2 is staged 20 cm behind DROP with the lift
            # already at height 2. When the logical location is also DROP,
            # begin with Marker 42 acquisition without another move/backoff.
            prepositioned_drop_pickup = (
                pickup == 'DROP' and self.current_code == 'DROP'
            )
            if prepositioned_drop_pickup:
                dock_command = 'START_SEARCH'
            else:
                pickup_lift, pickup_state, pickup_label = self.insertion_height(
                    pickup
                )
                self.feedback_transport(
                    goal_handle,
                    'PREPARING_PICKUP_HEIGHT',
                    0.02,
                    f'moving lift to {pickup_label}',
                )
                ok, error = self.lift(
                    pickup_lift, (pickup_state,), goal_handle
                )
                if not ok:
                    return self.step_failure(
                        goal_handle, result, 'PREPARING_PICKUP_HEIGHT', error
                    )

                self.feedback_transport(
                    goal_handle, 'MOVING_TO_PICKUP', 0.05,
                    f'moving to {pickup}'
                )
                ok, error, dock_command = self.move_to_code(
                    pickup, goal_handle
                )
                if not ok:
                    return self.step_failure(
                        goal_handle, result, 'MOVING_TO_PICKUP', error
                    )

            self.feedback_transport(
                goal_handle, 'DOCKING_PICKUP', 0.18, f'docking at {pickup}'
            )
            ok, error = self.dock(
                self.marker_id(pickup), dock_command, goal_handle
            )
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'DOCKING_PICKUP', error
                )

            self.feedback_transport(
                goal_handle,
                'LIFTING_TO_CARRY',
                0.30,
                'lifting pallet to height 3',
            )
            ok, error = self.lift(
                'HEIGHT_3', ('AT_HEIGHT_3',), goal_handle
            )
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'LIFTING_TO_CARRY', error
                )

            self.feedback_transport(
                goal_handle, 'LEAVING_PICKUP', 0.40, f'backing out of {pickup}'
            )
            ok, error = self.dock(
                self.marker_id(pickup), self.backoff_command(pickup), goal_handle
            )
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'LEAVING_PICKUP', error
                )
            self.current_code = pickup

            self.feedback_transport(
                goal_handle,
                'MOVING_TO_DROPOFF',
                0.52,
                f'moving from {pickup} to {dropoff} via HOME',
            )
            ok, error, dock_command = self.move_to_code(dropoff, goal_handle)
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'MOVING_TO_DROPOFF', error
                )

            self.feedback_transport(
                goal_handle, 'DOCKING_DROPOFF', 0.70, f'docking at {dropoff}'
            )
            ok, error = self.dock(
                self.marker_id(dropoff), dock_command, goal_handle
            )
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'DOCKING_DROPOFF', error
                )

            drop_lift, drop_state, drop_label = self.insertion_height(dropoff)
            self.feedback_transport(
                goal_handle,
                'LOWERING_TO_PLACE',
                0.83,
                f'lowering pallet to {drop_label}',
            )
            ok, error = self.lift(drop_lift, (drop_state,), goal_handle)
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'LOWERING_TO_PLACE', error
                )

            self.feedback_transport(
                goal_handle, 'LEAVING_DROPOFF', 0.93, f'backing out of {dropoff}'
            )
            ok, error = self.dock(
                self.marker_id(dropoff),
                self.backoff_command(dropoff),
                goal_handle,
            )
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'LEAVING_DROPOFF', error
                )
            self.current_code = dropoff

            self.feedback_transport(
                goal_handle, 'COMPLETED', 1.0, f'{pickup} to {dropoff} complete'
            )
            goal_handle.succeed()
            result.status = 'SUCCEEDED'
            result.error_code = ''
            result.detail = (
                f'transport complete: {pickup} -> {dropoff}; '
                f'current_location={self.current_code}'
            )
            return result
        except Exception as exc:
            return self.abort(goal_handle, result, 'INTERNAL_ERROR', str(exc))
        finally:
            self.release()

    def return_home(self, goal_handle):
        result = ReturnHome.Result()
        self.task_id = goal_handle.request.req_id.strip() or 'return-home'
        try:
            if not goal_handle.request.req_id.strip():
                return self.abort(
                    goal_handle, result, 'INVALID_GOAL', 'req_id is required'
                )
            self.feedback_home(
                goal_handle,
                'RETURNING_HOME',
                0.15,
                f'returning from {self.current_code} via validated route',
            )
            ok, error = self.return_to_home(goal_handle)
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'RETURNING_HOME', error
                )
            self.feedback_home(
                goal_handle, 'PARKING_LIFT', 0.90, 'parking lift'
            )
            ok, error = self.lift('PARK', ('PARKED',), goal_handle)
            if not ok:
                return self.step_failure(
                    goal_handle, result, 'PARKING_LIFT', error
                )
            final_backoff = bool(self.get_parameter(
                'return_home_final_backoff_enabled'
            ).value)
            if final_backoff:
                self.feedback_home(
                    goal_handle,
                    'FINAL_BACKOFF',
                    0.96,
                    'backing 0.20 m from aligned HOME position',
                )
                ok, error = self.dock(
                    self.marker_id('RACK1'), 'BACKOFF_HOME', goal_handle
                )
                if not ok:
                    return self.step_failure(
                        goal_handle, result, 'FINAL_BACKOFF', error
                    )
                self.current_code = 'HOME_BEHIND'
            self.feedback_home(goal_handle, 'COMPLETED', 1.0, 'home complete')
            goal_handle.succeed()
            result.status = 'SUCCEEDED'
            result.error_code = ''
            result.detail = (
                'aligned at HOME, parked lift, backed off 0.20 m; '
                'current_location=HOME_BEHIND'
                if final_backoff
                else 'returned HOME and parked lift; current_location=HOME'
            )
            return result
        except Exception as exc:
            return self.abort(goal_handle, result, 'INTERNAL_ERROR', str(exc))
        finally:
            self.release()

    def move_to_code(self, destination, goal_handle):
        if destination == self.current_code:
            return True, '', 'START'
        if self.current_code != 'HOME':
            ok, error = self.return_to_home(goal_handle)
            if not ok:
                return False, error, ''

        if destination == 'RACK1':
            self.current_code = 'RACK1'
            return True, '', 'START'
        if destination == 'RACK2':
            ok, error = self.route('GO_RACK2_LANE', goal_handle)
            if not ok:
                return False, error, ''
            self.current_code = 'RACK2'
            return True, '', 'START'
        if destination == 'DROP':
            ok, error = self.route('GO_HOME_BEHIND', goal_handle)
            if not ok:
                return False, error, ''
            self.current_code = 'DROP'
            return True, '', 'START_SEARCH'
        if destination == 'HOME':
            return True, '', ''
        return False, f'unsupported destination: {destination}', ''

    def return_to_home(self, goal_handle):
        # DROP 쪽 턱을 피하려면 HOME_BEHIND를 반드시 경유한다. 다만
        # HOME_BEHIND에서 곧바로 ArUco 정렬을 시작하면 HOME까지 남은
        # 거리를 전부 저속 시각 제어로 이동해 오래 걸리고 마커 유실이
        # 잦다. 안전 경유점 이후 HOME까지는 odom으로 먼저 이동하고,
        # ArUco는 마지막 누적 오차만 보정한다.
        if self.current_code == 'HOME':
            route_commands = ()
        else:
            route_commands = (
                ('GO_HOME_BEHIND', 'GO_HOME')
                if self.current_code == 'DROP'
                else ('GO_HOME',)
            )
        for route_command in route_commands:
            ok, error = self.route(route_command, goal_handle)
            if not ok:
                return False, error
        ok, error = self.dock(
            self.marker_id('RACK1'), 'ALIGN_HOME_FAST_SEARCH', goal_handle
        )
        if not ok:
            error = f'HOME 정렬 안전 정지: {error}'
        if not ok:
            return False, error
        self.current_code = 'HOME'
        return True, ''

    def wait_for_subscriber(self, publisher, goal_handle, label):
        deadline = time.monotonic() + float(
            self.get_parameter('endpoint_wait_sec').value
        )
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                return False, 'cancel requested'
            if publisher.get_subscription_count() > 0:
                return True, ''
            time.sleep(0.05)
        return False, f'no subscriber for {label}'

    def route(self, command, goal_handle):
        if bool(self.get_parameter('use_mock_route').value):
            return self.mock_wait(
                float(self.get_parameter('mock_route_delay_sec').value),
                goal_handle,
            )
        ok, error = self.wait_for_subscriber(
            self.route_pub, goal_handle, '/forklift/route/command'
        )
        if not ok:
            return False, error
        operation_id = self.new_operation_id(f'route-{command.lower()}')
        self.route_result = None
        self.route_event.clear()
        self.route_operation_id = operation_id
        self.route_pub.publish(
            ForkliftControlCommand(command_id=operation_id, command=command)
        )
        return self.wait_terminal(
            self.route_event,
            goal_handle,
            float(self.get_parameter('route_timeout_sec').value),
            lambda: self.route_result,
            f'route timeout: {command}',
        )

    def dock(self, marker_id, command, goal_handle):
        ok, error = self.wait_for_subscriber(
            self.dock_pub, goal_handle, '/forklift/dock/command'
        )
        if not ok:
            return False, error
        operation_id = self.new_operation_id(f'dock-{command.lower()}')
        self.dock_result = None
        self.dock_event.clear()
        self.dock_operation_id = operation_id
        self.dock_pub.publish(
            DockCommand(
                task_id=operation_id,
                marker_id=int(marker_id),
                command=command,
            )
        )
        return self.wait_terminal(
            self.dock_event,
            goal_handle,
            float(self.get_parameter('device_timeout_sec').value),
            lambda: self.dock_result,
            f'docking timeout: marker={marker_id}, command={command}',
        )

    def lift(self, command, expected, goal_handle):
        if bool(self.get_parameter('use_mock_lift').value):
            self.get_logger().info(f'lift mock success: {command}')
            return self.mock_wait(
                float(self.get_parameter('mock_lift_delay_sec').value),
                goal_handle,
            )
        ok, error = self.wait_for_subscriber(
            self.lift_pub, goal_handle, '/forklift/lift/command'
        )
        if not ok:
            return False, error
        operation_id = self.new_operation_id(f'lift-{command.lower()}')
        self.lift_result = None
        self.lift_event.clear()
        self.lift_operation_id = operation_id
        self.lift_pub.publish(
            LiftCommand(task_id=operation_id, command=command)
        )
        deadline = time.monotonic() + float(
            self.get_parameter('device_timeout_sec').value
        )
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                return False, 'cancel requested'
            if self.lift_event.wait(0.05):
                state, message = self.lift_result
                if state in ('FAILED', 'ERROR'):
                    return False, message
                if state in expected:
                    return True, ''
                self.lift_event.clear()
        return False, f'lift timeout: {command}'

    def on_route_state(self, msg):
        if msg.command_id != self.route_operation_id:
            return
        state = msg.state.strip().upper()
        if state in ('SUCCEEDED', 'FAILED', 'ERROR', 'STOPPED', 'CANCELED'):
            self.route_result = (
                state == 'SUCCEEDED' and bool(msg.success),
                msg.message,
            )
            self.route_event.set()

    def on_dock_state(self, msg):
        if msg.task_id != self.dock_operation_id:
            return
        state = msg.state.strip().upper()
        if state in ('SUCCEEDED', 'FAILED', 'ERROR'):
            self.dock_result = (state == 'SUCCEEDED', msg.message)
            self.dock_event.set()

    def on_lift_state(self, msg):
        if msg.task_id != self.lift_operation_id:
            return
        self.lift_result = (msg.state.strip().upper(), msg.message)
        self.lift_event.set()

    def wait_terminal(
        self, event, goal_handle, timeout, result_getter, timeout_message
    ):
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                return False, 'cancel requested'
            if event.wait(0.05):
                result = result_getter()
                if result is None:
                    event.clear()
                    continue
                return bool(result[0]), result[1]
        return False, timeout_message

    @staticmethod
    def mock_wait(delay, goal_handle):
        deadline = time.monotonic() + float(delay)
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                return False, 'cancel requested'
            time.sleep(0.02)
        return True, ''

    def new_operation_id(self, suffix):
        self.operation_sequence += 1
        base = re.sub(r'[^A-Za-z0-9_.-]+', '-', self.task_id)[:48]
        return f'{base}-{self.operation_sequence}-{suffix}'

    def stop_devices(self):
        stop_id = self.new_operation_id('stop')
        self.route_pub.publish(
            ForkliftControlCommand(command_id=stop_id, command='STOP')
        )
        self.dock_pub.publish(
            DockCommand(task_id=stop_id, marker_id=0, command='STOP')
        )
        if not bool(self.get_parameter('use_mock_lift').value):
            self.lift_pub.publish(
                LiftCommand(task_id=stop_id, command='STOP')
            )

    def step_failure(self, goal_handle, result, phase, error):
        if goal_handle.is_cancel_requested:
            self.cancel_result(goal_handle, result)
            return result
        return self.abort(
            goal_handle, result, 'STEP_FAILED', f'{phase}: {error}'
        )

    def cancel_result(self, goal_handle, result):
        self.stop_devices()
        goal_handle.canceled()
        result.status = 'CANCELED'
        result.error_code = 'CANCELED'
        result.detail = 'user canceled the action'

    @staticmethod
    def abort(goal_handle, result, code, detail):
        goal_handle.abort()
        result.status = 'FAILED'
        result.error_code = code
        result.detail = detail
        return result

    def release(self):
        self.task_id = ''
        self.route_operation_id = ''
        self.dock_operation_id = ''
        self.lift_operation_id = ''
        with self.state_lock:
            self.active = False

    @staticmethod
    def feedback_transport(goal_handle, phase, progress, detail):
        goal_handle.publish_feedback(
            ExecuteTransport.Feedback(
                phase=phase, progress=float(progress), detail=detail
            )
        )

    @staticmethod
    def feedback_home(goal_handle, phase, progress, detail):
        goal_handle.publish_feedback(
            ReturnHome.Feedback(
                phase=phase, progress=float(progress), detail=detail
            )
        )

    def destroy_node(self):
        self.execute_server.destroy()
        self.home_server.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TransportActionServer()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
