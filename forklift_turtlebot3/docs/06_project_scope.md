# 06. 프로젝트 범위

## 이 모듈이 담당하는 것

- TurtleBot3 고정 경로 주행과 작업점 경유 규칙
- ArUco 검출, 정렬, 도킹과 후진
- Raspberry Pi GPIO 리프트 제어
- 자동 운반과 HOME 복귀 ROS 2 ActionServer
- Unity 직접 수동주행 WebSocket
- 자동·수동 cmd_vel 중재와 데드맨 정지
- 현장 경로·도킹 캘리브레이션

## 이 모듈이 담당하지 않는 것

- 생산 작업과 배송 요청 생성
- Main Server의 DB와 스케줄링
- Unity 화면과 조작 UI 구현
- 협동로봇과 비전 검사 내부 구현
- SLAM 지도 생성과 Nav2 경로계획
- 인터넷 구간 인증·원격접속

## 외부 계약

Main Server는 pickup_code와 dropoff_code를 포함한 ExecuteTransport Action을 보내고 최종
result로 완료 여부를 판단합니다. 좌표와 마커 ID는 보내지 않습니다.

Unity는 Pi의 WebSocket으로 수동 속도 명령을 보내며 ROS 2 메시지를 직접 구성할 필요가
없습니다. 현재 토큰 인증은 사용하지 않으므로 같은 팀 전용 LAN에서만 운용합니다.

[문서 목록](README.md) · [이전](05_validation.md) · [다음](07_project_structure.md)
