# 02. 시스템 아키텍처

## 실행 배치

```text
Main Server                         Unity
     | ROS 2 Action                    | WebSocket :8765
     v                                 v
+---------------- PC ----------------+   +------------- Raspberry Pi -------------+
| transport_action_server           |   | manual_websocket_gateway               |
|      | route/dock/lift commands    |   |              | manual cmd_vel          |
| odom_axis_driver                   |-->| cmd_vel_arbiter ----------------> /cmd_vel
| aruco_detector -> aruco_docking    |   | lift_servo <-> fk_jogd.py -> GPIO lift |
+------------------------------------+   | TurtleBot bringup / camera             |
                                         +-----------------------------------------+
```

## 주요 노드

| 노드 | 위치 | 역할 |
|:---|:---:|:---|
| transport_action_server | PC | 운반과 HOME 복귀 상태기계 |
| odom_axis_driver | PC | 고정 경로 주행과 장애물 정지 |
| aruco_detector | PC | 카메라 영상에서 마커 pose 검출 |
| aruco_docking | PC | 마커 기반 정렬·접근·후진 |
| lift_servo | Pi | ROS LiftCommand를 UDP 데몬 명령으로 변환 |
| cmd_vel_arbiter | Pi | 자동·수동 속도 명령의 단일 /cmd_vel 출력 |
| manual_websocket_gateway | Pi | Unity 수동주행 WebSocket |
| fk_jogd.py | Pi | GPIO 스테퍼 구동과 프리셋 저장 |

## 속도 명령 소유권

경로 노드는 /forklift/cmd_vel/route, 도킹 노드는 /forklift/cmd_vel/dock,
Unity는 /forklift/cmd_vel/manual 계열로 명령을 보냅니다. 실제 /cmd_vel은 Pi의
cmd_vel_arbiter만 발행합니다. 연결 또는 명령이 끊기면 0 속도로 정지합니다.

[문서 목록](README.md) · [이전](01_overview.md) · [다음](03_features.md)
