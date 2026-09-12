# Unity → Raspberry Pi 직접 수동주행

이 기능은 Main Server를 통하지 않고 Unity가 Raspberry Pi의 WebSocket에 직접
연결해 TurtleBot을 수동으로 움직이기 위한 비상·보조 기능입니다. 리프트는 제어하지
않고 전진·후진·회전·정지만 허용합니다.

## 연결

- 주소: `ws://<PI_IP>:8765/manual`
- 현재 Pi 고정 IP를 쓰는 경우: `ws://192.168.20.100:8765/manual`
- 연결 가능 클라이언트: 동시에 1개
- 메시지 인코딩: UTF-8 JSON object
- 권장 전송 주기: `manual_drive` 20 Hz (최소 10 Hz)

Pi와 Unity 장치는 같은 신뢰된 LAN에 있어야 합니다. 현재 팀 운용에서는
`manual_ws_token`을 비워두며 요청에 `token` 필드를 넣지 않습니다.

## 제어 순서

1. WebSocket 연결
2. `TAKE_CONTROL` 전송
3. 응답 `MANUAL_READY` 확인
4. 데드맨 버튼을 누르는 동안 `manual_drive`를 10~20 Hz로 계속 전송
5. 버튼을 놓으면 즉시 `STOP` 전송
6. 수동조작이 끝나면 `RELEASE_CONTROL` 전송 후 연결 종료

`TAKE_CONTROL` 시 진행 중인 route, docking, lift에 STOP을 보냅니다. 수동모드 중에는
새 자동운반 Action을 거절합니다. `RELEASE_CONTROL` 후 기존 자동 작업은 저절로
재개되지 않습니다.

## Unity → Pi 메시지

세션 ID는 연결마다 UUID를 새로 생성하는 것을 권장합니다.

제어권 획득:

```json
{
  "type": "manual_control",
  "command": "TAKE_CONTROL",
  "session_id": "unity-550e8400-e29b-41d4-a716-446655440000"
}
```

주행 명령:

```json
{
  "type": "manual_drive",
  "session_id": "unity-550e8400-e29b-41d4-a716-446655440000",
  "sequence": 1,
  "linear_x": 0.05,
  "angular_z": 0.0,
  "deadman": true
}
```

- `sequence`: 같은 세션에서 반드시 증가하는 정수
- `linear_x`: m/s, Pi에서 `-0.05 ~ +0.08`로 제한
- `angular_z`: rad/s, Pi에서 `-0.35 ~ +0.35`로 제한
- `deadman`: 반드시 JSON boolean `true`

정지:

```json
{
  "type": "manual_control",
  "command": "STOP",
  "session_id": "unity-550e8400-e29b-41d4-a716-446655440000"
}
```

제어권 반환:

```json
{
  "type": "manual_control",
  "command": "RELEASE_CONTROL",
  "session_id": "unity-550e8400-e29b-41d4-a716-446655440000"
}
```

## Pi → Unity 메시지

연결 직후:

```json
{"type":"connection","state":"CONNECTED","message":"TAKE_CONTROL을 먼저 보내세요"}
```

상태 응답:

```json
{
  "type": "manual_state",
  "session_id": "unity-550e8400-e29b-41d4-a716-446655440000",
  "state": "MANUAL_READY",
  "success": true,
  "message": "Unity 수동조작 준비 완료"
}
```

상태 값은 `AUTO`, `MANUAL_READY`, `MANUAL_ACTIVE`, `STOPPED`, `TIMED_OUT`,
`REJECTED`입니다. 오류 메시지는 다음 형식입니다.

```json
{"type":"error","code":"take_control_first"}
```

## 안전 동작

- `manual_drive`가 0.30초 끊기면 Pi가 0 속도를 출력하고 `TIMED_OUT`으로 잠깁니다.
- 타임아웃 후에는 같은 세션으로 `TAKE_CONTROL`을 다시 보내야 주행할 수 있습니다.
- WebSocket 연결이 끊기면 Pi가 `STOP`, 이어서 `RELEASE_CONTROL`을 적용합니다.
- 순서가 뒤로 간 패킷, 세션이 다른 패킷, NaN/Infinity, 데드맨이 꺼진 패킷은 거부합니다.
- 실제 `/cmd_vel`은 `cmd_vel_arbiter` 하나만 발행합니다.

## Pi 설치 및 실행

처음 한 번 WebSocket 의존성을 설치합니다.

```bash
sudo apt update
sudo apt install python3-websockets
```

PC에서 변경분을 Pi에 배포한 뒤 Pi에서 깨끗하게 빌드합니다.

```bash
cd ~/forklift_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select forklift_interfaces forklift_control --symlink-install
source install/setup.bash
```

Pi 전용 속도 중재기와 WebSocket launch를 실행합니다. 이 launch는 자동주행의
속도도 실제 `/cmd_vel`로 전달하므로 Unity를 사용하지 않을 때도 Pi에서 계속
실행해야 합니다. 현재 팀 운용은 인증 토큰을 사용하지 않으며 같은 로컬 네트워크에서만 연결합니다.

```bash
ros2 launch forklift_control forklift_pi.launch.py
```

TCP 8765를 인터넷이나 공유기 포트포워딩으로 노출하지 마세요.

상태 확인:

```bash
ros2 node list | grep -E 'cmd_vel_arbiter|manual_websocket_gateway'
ros2 topic echo /forklift/manual/state
ss -ltn | grep 8765
```

방화벽을 사용 중이면 Unity 장치에서 들어오는 TCP 8765만 허용합니다. 인터넷에
8765 포트를 노출하거나 공유기 포트포워딩을 설정하지 마세요.
