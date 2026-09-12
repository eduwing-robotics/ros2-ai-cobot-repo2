#!/usr/bin/env python3
"""Direct Unity-to-Pi WebSocket gateway for guarded manual driving."""

import asyncio
import hmac
import json
import math
import queue
import threading

import rclpy
from geometry_msgs.msg import TwistStamped
from rclpy.node import Node

from forklift_interfaces.msg import ForkliftControlCommand, ForkliftTaskStatus

try:
    import websockets
except ImportError:  # Report a useful ROS log instead of an obscure import failure.
    websockets = None


class ManualWebSocketGateway(Node):
    """Translate a small JSON protocol into the ROS manual-control topics."""

    def __init__(self):
        super().__init__('manual_websocket_gateway')
        defaults = (
            ('manual_ws_bind_host', '0.0.0.0'),
            ('manual_ws_port', 8765),
            ('manual_ws_token', ''),
            ('max_forward_mps', 0.08),
            ('max_reverse_mps', 0.05),
            ('max_angular_rps', 0.35),
        )
        for name, value in defaults:
            self.declare_parameter(name, value)

        if websockets is None:
            raise RuntimeError(
                'Python websockets 패키지가 없습니다: '
                'sudo apt install python3-websockets'
            )

        self.bind_host = str(
            self.get_parameter('manual_ws_bind_host').value
        )
        self.port = int(self.get_parameter('manual_ws_port').value)
        self.token = str(self.get_parameter('manual_ws_token').value)
        self.max_forward = float(self.get_parameter('max_forward_mps').value)
        self.max_reverse = float(self.get_parameter('max_reverse_mps').value)
        self.max_angular = float(self.get_parameter('max_angular_rps').value)

        self.control_pub = self.create_publisher(
            ForkliftControlCommand, '/forklift/manual/control', 10
        )
        self.velocity_pub = self.create_publisher(
            TwistStamped, '/forklift/manual/cmd_vel', 10
        )
        self.create_subscription(
            ForkliftTaskStatus,
            '/forklift/manual/state',
            self.on_manual_state,
            10,
        )

        self.incoming = queue.Queue(maxsize=100)
        self.client = None
        self.client_session = ''
        self.last_sequence = -1
        self.loop = asyncio.new_event_loop()
        self.server_ready = threading.Event()
        self.server_error = None
        self.thread = threading.Thread(target=self.run_server, daemon=True)
        self.thread.start()
        self.create_timer(0.01, self.drain_incoming)

        if not self.token:
            self.get_logger().warning(
                'manual_ws_token이 비어 있습니다. 신뢰할 수 있는 전용 LAN에서만 사용하세요.'
            )
        self.get_logger().info(
            f'Unity 수동 WebSocket 준비 중: ws://{self.bind_host}:{self.port}/manual'
        )

    def run_server(self):
        asyncio.set_event_loop(self.loop)

        async def start():
            try:
                server = await websockets.serve(
                    self.handle_client,
                    self.bind_host,
                    self.port,
                    ping_interval=10,
                    ping_timeout=5,
                    max_size=16 * 1024,
                )
                self.server_ready.set()
                return server
            except Exception as exc:
                self.server_error = exc
                self.server_ready.set()
                return None

        server = self.loop.run_until_complete(start())
        if server is None:
            return
        self.loop.run_forever()
        server.close()
        self.loop.run_until_complete(server.wait_closed())

    @staticmethod
    def websocket_path(websocket, path):
        if path is not None:
            return path
        request = getattr(websocket, 'request', None)
        return getattr(request, 'path', None) or getattr(websocket, 'path', '')

    async def handle_client(self, websocket, path=None):
        if self.websocket_path(websocket, path) != '/manual':
            await websocket.close(code=1008, reason='use /manual')
            return
        if self.client is not None:
            await websocket.close(code=1013, reason='manual client already connected')
            return

        self.client = websocket
        self.client_session = ''
        self.last_sequence = -1
        await self.send_to(websocket, {
            'type': 'connection',
            'state': 'CONNECTED',
            'message': 'TAKE_CONTROL을 먼저 보내세요',
        })
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    await self.send_to(websocket, self.error('invalid_json'))
                    continue
                if not isinstance(message, dict):
                    await self.send_to(websocket, self.error('object_required'))
                    continue
                if not self.authorized(message):
                    await self.send_to(websocket, self.error('unauthorized'))
                    await websocket.close(code=1008, reason='unauthorized')
                    break
                validation_error = self.validate_message(message)
                if validation_error:
                    await self.send_to(websocket, self.error(validation_error))
                    continue
                self.enqueue(message)
        finally:
            session = self.client_session
            if self.client is websocket:
                self.client = None
                self.client_session = ''
                self.last_sequence = -1
            if session:
                self.enqueue({
                    'type': 'manual_control',
                    'command': 'STOP',
                    'session_id': session,
                })
                self.enqueue({
                    'type': 'manual_control',
                    'command': 'RELEASE_CONTROL',
                    'session_id': session,
                })

    def authorized(self, message):
        if not self.token:
            return True
        supplied = str(message.get('token', ''))
        return hmac.compare_digest(supplied, self.token)

    def validate_message(self, message):
        msg_type = str(message.get('type', '')).lower()
        session = str(
            message.get('session_id') or message.get('request_id') or ''
        ).strip()
        if msg_type == 'manual_control':
            command = str(message.get('command', '')).upper()
            if command not in ('TAKE_CONTROL', 'STOP', 'RELEASE_CONTROL'):
                return 'unsupported_control_command'
            if not session:
                return 'session_id_required'
            if command == 'TAKE_CONTROL':
                if self.client_session and self.client_session != session:
                    return 'session_already_active'
                self.client_session = session
                self.last_sequence = -1
            elif not self.client_session or session != self.client_session:
                return 'session_mismatch'
            if command == 'RELEASE_CONTROL':
                self.client_session = ''
                self.last_sequence = -1
            return ''

        if msg_type == 'manual_drive':
            if not session or session != self.client_session:
                return 'take_control_first'
            if message.get('deadman') is not True:
                return 'deadman_must_be_true'
            sequence = message.get('sequence')
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                return 'integer_sequence_required'
            if sequence <= self.last_sequence:
                return 'sequence_not_increasing'
            try:
                linear = float(message.get('linear_x'))
                angular = float(message.get('angular_z'))
            except (TypeError, ValueError):
                return 'numeric_velocity_required'
            if not math.isfinite(linear) or not math.isfinite(angular):
                return 'finite_velocity_required'
            self.last_sequence = sequence
            return ''
        return 'unsupported_message_type'

    def enqueue(self, message):
        try:
            self.incoming.put_nowait(message)
        except queue.Full:
            while not self.incoming.empty():
                try:
                    self.incoming.get_nowait()
                except queue.Empty:
                    break
            session = self.client_session or str(message.get('session_id', ''))
            self.incoming.put_nowait({
                'type': 'manual_control',
                'command': 'STOP',
                'session_id': session,
            })

    def drain_incoming(self):
        if self.server_error is not None:
            error = self.server_error
            self.server_error = None
            self.get_logger().error(f'WebSocket 서버 시작 실패: {error}')
        for _ in range(50):
            try:
                message = self.incoming.get_nowait()
            except queue.Empty:
                return
            msg_type = str(message.get('type', '')).lower()
            if msg_type == 'manual_control':
                self.control_pub.publish(ForkliftControlCommand(
                    command_id=str(message.get('session_id', '')),
                    command=str(message.get('command', '')).upper(),
                ))
            elif msg_type == 'manual_drive':
                msg = TwistStamped()
                msg.header.stamp = self.get_clock().now().to_msg()
                # 중재 노드가 현재 TAKE_CONTROL 세션만 허용하도록 세션을 전달합니다.
                msg.header.frame_id = str(message.get('session_id', ''))
                linear = float(message['linear_x'])
                angular = float(message['angular_z'])
                msg.twist.linear.x = max(
                    -self.max_reverse, min(self.max_forward, linear)
                )
                msg.twist.angular.z = max(
                    -self.max_angular, min(self.max_angular, angular)
                )
                self.velocity_pub.publish(msg)

    def on_manual_state(self, msg):
        self.send_json({
            'type': 'manual_state',
            'session_id': msg.command_id,
            'state': msg.state,
            'success': msg.success,
            'message': msg.message,
        })

    @staticmethod
    def error(code):
        return {'type': 'error', 'code': code}

    async def send_to(self, websocket, payload):
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    def send_json(self, payload):
        client = self.client
        if client is None or self.loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self.send_to(client, payload), self.loop
        )

        def log_failure(done):
            try:
                done.result()
            except Exception:
                pass

        future.add_done_callback(log_failure)

    def destroy_node(self):
        client = self.client
        if client is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                client.close(code=1001, reason='ROS node shutdown'), self.loop
            )
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = ManualWebSocketGateway()
        rclpy.spin(node)
    except RuntimeError as exc:
        print(f'manual_websocket_gateway: {exc}')
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
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
