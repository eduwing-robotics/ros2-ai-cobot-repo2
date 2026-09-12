#!/usr/bin/env python3
"""ROS 2 adapter for the Raspberry Pi UDP stepper-lift daemon."""

import json
import socket
import time

import rclpy
from rclpy.node import Node

from forklift_interfaces.msg import LiftCommand, LiftState


class LiftUdpAdapter(Node):
    """Translate LiftCommand messages to the fk_jogd.py UDP protocol."""

    def __init__(self):
        # Match config/forklift_params.yaml when run without the launch file.
        super().__init__('forklift_lift_servo')
        defaults = (
            ('udp_host', '127.0.0.1'),
            ('udp_port', 5005),
            ('request_timeout_sec', 0.35),
            ('poll_period_sec', 0.25),
            ('motion_timeout_sec', 70.0),
            ('position_tolerance_mm', 0.5),
            # Physical workflow presets:
            # J2=RACK/DROP insertion/release, J3=carrying height.
            # HEIGHT_1 is retained only as a compatibility alias for J2.
            ('height_1_preset', 2),
            ('height_2_preset', 2),
            ('height_3_preset', 3),
            # Legacy command aliases kept for manual/older clients.
            ('up_preset', 3),
            ('down_preset', 2),
            ('park_preset', 2),
            ('drop_release_mm', 10.0),
            ('drop_j1_reference_mm', 0.0),
            ('drop_j1_tolerance_mm', 1.0),
            ('position_direction_sign', 1.0),
            ('require_zero_before_motion', True),
            ('use_mock_lift', False),
            # Backward-compatible launch argument from the old servo node.
            ('use_mock_gpio', False),
            ('mock_motion_time_sec', 0.5),
        )
        for name, value in defaults:
            self.declare_parameter(name, value)

        self.host = str(self.get_parameter('udp_host').value)
        self.port = int(self.get_parameter('udp_port').value)
        self.timeout = float(self.get_parameter('request_timeout_sec').value)
        self.motion_timeout = float(self.get_parameter('motion_timeout_sec').value)
        self.tolerance_mm = float(
            self.get_parameter('position_tolerance_mm').value
        )
        self.mock = bool(self.get_parameter('use_mock_lift').value) or bool(
            self.get_parameter('use_mock_gpio').value
        )
        self.require_zero = bool(
            self.get_parameter('require_zero_before_motion').value
        )
        self.socket = None
        if not self.mock:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.settimeout(self.timeout)

        self.sequence = 0
        self.task_id = ''
        self.homed = self.mock or not self.require_zero
        self.busy = False
        self.target_preset = None
        self.target_mm = None
        self.target_label = ''
        self.success_state = ''
        self.motion_started = 0.0
        self.communication_failures = 0
        self.mock_finish_timer = None
        self.drop_original_limits = None

        self.state_pub = self.create_publisher(
            LiftState, '/forklift/lift/state', 10
        )
        self.create_subscription(
            LiftCommand, '/forklift/lift/command', self.on_command, 10
        )
        self.poll_timer = self.create_timer(
            float(self.get_parameter('poll_period_sec').value), self.poll
        )

        initial = 'AT_HEIGHT_2' if self.homed else 'UNHOMED'
        message = (
            '모의 리프트 준비 완료'
            if self.mock
            else '리프트가 실제 HEIGHT_2인지 확인한 뒤 ZERO 명령을 보내세요'
        )
        self.publish_state(initial, message)

    def on_command(self, msg):
        command = msg.command.strip().upper()
        self.task_id = msg.task_id

        if command in ('STOP', 'S'):
            self.stop('STOPPED', '정지 명령 완료')
            return
        if command in ('STATUS', '?'):
            self.publish_current_status()
            return
        if command in ('ZERO', 'Z'):
            self.zero()
            return

        if command in ('DROP_LOWER', 'DROP_RAISE'):
            if self.busy:
                self.publish_state('ERROR', '리프트가 이미 움직이는 중입니다')
                return
            if not self.homed:
                self.publish_state(
                    'ERROR', '영점 미확인: 기준 주차 높이에서 ZERO를 먼저 보내세요'
                )
                return
            delta_mm = float(
                self.get_parameter('drop_release_mm').value
            )
            if command == 'DROP_LOWER':
                delta_mm = -delta_mm
                success_state = 'DROP_LOWERED'
            else:
                success_state = 'DROP_RAISED'
            self.start_relative(delta_mm, success_state)
            return

        preset, success_state = self.parse_motion_command(command)
        if preset is None:
            self.publish_state('ERROR', f'지원하지 않는 명령: {msg.command}')
            return
        if self.busy:
            self.publish_state('ERROR', '리프트가 이미 움직이는 중입니다')
            return
        if not self.homed:
            self.publish_state(
                'ERROR', '영점 미확인: 기준 주차 높이에서 ZERO를 먼저 보내세요'
            )
            return
        self.start_preset(preset, success_state)

    def parse_motion_command(self, command):
        height_commands = {
            'HEIGHT_1': 'height_1_preset',
            'HEIGHT_2': 'height_2_preset',
            'HEIGHT_3': 'height_3_preset',
        }
        if command in height_commands:
            preset = int(self.get_parameter(height_commands[command]).value)
            return preset, f'AT_{command}'
        if command == 'UP':
            return int(self.get_parameter('up_preset').value), 'UP'
        if command == 'DOWN':
            return int(self.get_parameter('down_preset').value), 'DOWN'
        if command == 'PARK':
            return int(self.get_parameter('park_preset').value), 'PARKED'
        text = command.removeprefix('PRESET_').removeprefix('J')
        if text in ('1', '2', '3', '4'):
            preset = int(text)
            return preset, f'AT_PRESET_{preset}'
        return None, ''

    def zero(self):
        if self.busy:
            self.publish_state('ERROR', '이동 중에는 기준 위치를 설정할 수 없습니다')
            return
        if self.homed and not self.mock:
            self.publish_state(
                'ERROR', '이미 HEIGHT_2 기준 설정됨: 다시 하려면 어댑터를 재시작하세요'
            )
            return
        if self.mock:
            self.homed = True
            self.publish_state('AT_HEIGHT_2', '[MOCK] 현재 위치를 HEIGHT_2로 확인')
            return
        try:
            self.exchange('S')
            status = self.exchange('?')
            memories = status.get('mem') or []
            height_2_preset = int(
                self.get_parameter('height_2_preset').value
            )
            if (
                len(memories) < height_2_preset
                or memories[height_2_preset - 1] is None
            ):
                raise ValueError('HEIGHT_2 프리셋이 데몬에 저장되지 않음')
            height_2_steps = int(memories[height_2_preset - 1])
            status = self.exchange(f'Y{height_2_steps}')
            note = str(status.get('note', ''))
            if note.startswith('!'):
                raise ValueError(note)
            reported_steps = int(status.get('pos', height_2_steps))
            if reported_steps != height_2_steps:
                raise ValueError(
                    f'HEIGHT_2 위치 복구 불일치: {reported_steps}!={height_2_steps}'
                )
        except (OSError, ValueError, TimeoutError) as exc:
            self.homed = False
            self.publish_state('ERROR', f'HEIGHT_2 기준 설정 실패: {exc}')
            return
        self.homed = True
        self.communication_failures = 0
        self.publish_state(
            'AT_HEIGHT_2',
            f'현재 위치를 HEIGHT_2로 확인 ({height_2_steps}스텝, '
            f"{float(status.get('mm', 0.0)):.1f}mm)",
        )

    def start_relative(self, delta_mm, success_state):
        self.busy = True
        self.target_preset = None
        self.target_label = 'DROP 상대 높이'
        self.success_state = success_state
        self.motion_started = time.monotonic()
        self.communication_failures = 0
        self.target_mm = None
        direction = '하강' if delta_mm < 0.0 else '상승'
        self.publish_state(
            f'MOVING_{success_state}',
            f'DROP 이탈용 {abs(delta_mm):.1f}mm {direction} 시작',
        )

        if self.mock:
            self.target_mm = float(delta_mm)
            delay = float(self.get_parameter('mock_motion_time_sec').value)
            self.mock_finish_timer = self.create_timer(delay, self.finish_mock)
            return

        try:
            current = self.exchange('?')
            current_mm = float(current.get('mm', 0.0))
            reference_mm = float(
                self.get_parameter('drop_j1_reference_mm').value
            )
            reference_tolerance = float(
                self.get_parameter('drop_j1_tolerance_mm').value
            )
            if (
                success_state == 'DROP_LOWERED'
                and abs(current_mm - reference_mm) > reference_tolerance
            ):
                raise ValueError(
                    'DROP_LOWER는 DOWN 완료 후 J1 높이에서만 실행할 수 있음 '
                    f'(현재={current_mm:.1f}mm, J1={reference_mm:.1f}mm)'
                )

            self.target_mm = current_mm + float(delta_mm)
            if success_state == 'DROP_LOWERED':
                if not bool(current.get('limit_on', False)):
                    raise ValueError(
                        '리프트 하한/상한 리밋이 활성화되지 않아 추가 하강 금지'
                    )
                lead_mm = float(current.get('lead', 0.0))
                if lead_mm <= 0.0:
                    raise ValueError('리프트 lead 값이 올바르지 않음')
                sign = 1 if float(
                    self.get_parameter('position_direction_sign').value
                ) >= 0.0 else -1
                target_steps = int(
                    round(self.target_mm / lead_mm * 4096.0)
                ) * sign
                original_low = int(current.get('lo', 0))
                original_high = int(current.get('hi', 0))
                temporary_low = min(original_low, target_steps)
                if original_high <= temporary_low:
                    raise ValueError('리프트 임시 하한을 설정할 수 없음')
                self.drop_original_limits = (
                    original_low, original_high
                )
                limit_status = self.exchange(
                    f'L{temporary_low}:{original_high}'
                )
                if str(limit_status.get('note', '')).startswith('!'):
                    raise ValueError(str(limit_status.get('note')))

            if (
                success_state == 'DROP_RAISED'
                and self.drop_original_limits is None
            ):
                self.drop_original_limits = (
                    0, int(current.get('hi', 0))
                )

            status = self.exchange(f'P{self.target_mm:.3f}')
            position_mm = float(status.get('mm', current_mm))
            if (
                int(status.get('dir', 0)) == 0
                and abs(position_mm - self.target_mm) > self.tolerance_mm
            ):
                raise ValueError(
                    f'요청 높이 {self.target_mm:.1f}mm가 리프트 한계를 벗어남'
                )
            self.handle_status(status)
        except (OSError, ValueError, TimeoutError) as exc:
            self.fail(f'DROP 상대 높이 이동 시작 실패: {exc}')

    def restore_drop_limits(self):
        if self.drop_original_limits is None or self.mock:
            return
        lower, upper = self.drop_original_limits
        status = self.exchange(f'L{lower}:{upper}')
        note = str(status.get('note', ''))
        if note.startswith('!'):
            raise ValueError(note)
        self.drop_original_limits = None

    def start_preset(self, preset, success_state):
        if not 1 <= preset <= 4:
            self.publish_state('ERROR', f'잘못된 프리셋 번호: {preset}')
            return
        self.busy = True
        self.target_preset = preset
        self.target_label = f'프리셋 {preset}'
        self.success_state = success_state
        self.motion_started = time.monotonic()
        self.communication_failures = 0
        self.target_mm = None
        self.publish_state(
            f'MOVING_PRESET_{preset}', f'리프트 프리셋 {preset} 이동 시작'
        )

        if self.mock:
            delay = float(self.get_parameter('mock_motion_time_sec').value)
            self.mock_finish_timer = self.create_timer(delay, self.finish_mock)
            return
        try:
            status = self.exchange(f'J{preset}')
            memories = status.get('mem_mm') or []
            if len(memories) < preset or memories[preset - 1] is None:
                raise ValueError(f'프리셋 {preset} 높이가 데몬에 저장되지 않음')
            self.target_mm = float(memories[preset - 1])
            self.handle_status(status)
        except (OSError, ValueError, TimeoutError) as exc:
            self.fail(f'프리셋 이동 시작 실패: {exc}')

    def poll(self):
        if self.mock or not self.busy:
            return
        if time.monotonic() - self.motion_started > self.motion_timeout:
            self.stop('ERROR', '리프트 이동 제한시간 초과')
            return
        try:
            status = self.exchange('?')
        except (OSError, ValueError, TimeoutError) as exc:
            self.communication_failures += 1
            if self.communication_failures >= 3:
                self.fail(f'리프트 UDP 통신 실패: {exc}')
            return
        self.communication_failures = 0
        self.handle_status(status)

    def handle_status(self, status):
        if status.get('err'):
            self.fail(str(status['err']))
            return
        note = str(status.get('note', ''))
        if note.startswith('!') or '실패' in note:
            self.fail(note)
            return
        if not self.busy or self.target_mm is None:
            return
        position_mm = float(status.get('mm', 0.0))
        direction = int(status.get('dir', 0))
        if direction == 0 and abs(position_mm - self.target_mm) <= self.tolerance_mm:
            if self.success_state == 'DROP_RAISED':
                try:
                    self.restore_drop_limits()
                except (OSError, ValueError, TimeoutError) as exc:
                    self.fail(f'DROP 하한 리밋 복구 실패: {exc}')
                    return
            self.busy = False
            self.publish_state(
                self.success_state,
                f'{self.target_label} 도착 ({position_mm:.1f}mm)',
            )

    def publish_current_status(self):
        if self.mock:
            state = (
                'MOVING'
                if self.busy
                else ('AT_HEIGHT_2' if self.homed else 'UNHOMED')
            )
            self.publish_state(state, '[MOCK] 상태 조회')
            return
        try:
            status = self.exchange('?')
        except (OSError, ValueError, TimeoutError) as exc:
            self.publish_state('ERROR', f'상태 조회 실패: {exc}')
            return
        state = 'MOVING' if int(status.get('dir', 0)) else 'IDLE'
        if not self.homed:
            state = 'UNHOMED'
        self.publish_state(
            state,
            f"{float(status.get('mm', 0.0)):.1f}mm, {status.get('note', '')}",
        )

    def finish_mock(self):
        self.mock_finish_timer.cancel()
        self.destroy_timer(self.mock_finish_timer)
        self.mock_finish_timer = None
        self.busy = False
        message = f'[MOCK] {self.target_label} 도착'
        self.publish_state(self.success_state, message)

    def stop(self, state, message):
        if self.mock:
            if self.mock_finish_timer is not None:
                self.mock_finish_timer.cancel()
                self.destroy_timer(self.mock_finish_timer)
                self.mock_finish_timer = None
        else:
            try:
                self.exchange('S')
            except (OSError, ValueError, TimeoutError) as exc:
                message = f'{message}; UDP 정지 전송 실패: {exc}'
                state = 'ERROR'
        self.busy = False
        self.target_mm = None
        self.target_label = ''
        self.publish_state(state, message)

    def fail(self, message):
        self.stop('ERROR', message)

    def exchange(self, command):
        self.sequence += 1
        sequence = str(self.sequence)
        payload = f'{sequence}|{command}'.encode()
        self.socket.sendto(payload, (self.host, self.port))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                data, _ = self.socket.recvfrom(4096)
            except socket.timeout as exc:
                raise TimeoutError(f'{self.host}:{self.port} 응답 시간 초과') from exc
            response = json.loads(data.decode('utf-8'))
            if str(response.get('seq', '')) == sequence:
                return response
        raise TimeoutError(f'{self.host}:{self.port} 응답 시간 초과')

    def publish_state(self, state, message):
        self.state_pub.publish(
            LiftState(task_id=self.task_id, state=state, message=message)
        )
        if state == 'ERROR':
            self.get_logger().error(f'{state}: {message}')
        else:
            self.get_logger().info(f'{state}: {message}')

    def destroy_node(self):
        if self.busy and rclpy.ok():
            self.stop('STOPPED', '노드 종료')
        if self.socket is not None:
            self.socket.close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = LiftUdpAdapter()
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
