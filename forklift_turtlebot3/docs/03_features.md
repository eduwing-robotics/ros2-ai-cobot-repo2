# 03. 주요 기능

## 자동 운반

ExecuteTransport Action의 pickup_code와 dropoff_code를 받아 두 지점 사이 운반을 수행합니다.
지원 작업 코드는 RACK1, RACK2, DROP입니다. 랙과 DROP 사이 이동에는 턱 회피를 위해
HOME 또는 HOME_BEHIND 경유 규칙이 적용됩니다.

## 정밀 도킹

- ArUco 40: RACK2
- ArUco 41: RACK1 및 HOME 기준
- ArUco 42: DROP
- 작업점별 실측 목표 pose와 staging pose 사용
- 마커 유실, 잘못된 이동 방향과 과도한 좌우 오차 시 정지
- 도킹·하역 후 20cm 직선 후진

## 리프트

- HEIGHT_2/J2: 약 17mm, 진입·배치·주차
- HEIGHT_3/J3: 약 35mm, 운반
- 데몬 재시작 후 실제 HEIGHT_2 확인과 ZERO 기준 복구 필수
- UDP 응답 sequence 검사와 이동 완료 상태 확인

## HOME 복귀

ReturnHome Action은 HOME 접근, ArUco 41 정렬, HEIGHT_2 주차, 20cm 후진을 순서대로
수행하고 HOME_BEHIND 상태에서 종료합니다.

## Unity 직접 수동주행

Main Server 없이 Unity가 ws://<PI_IP>:8765/manual로 접속할 수 있습니다. 현재 팀 운용은
인증 토큰을 사용하지 않으며 같은 로컬 네트워크에서만 허용합니다. 데드맨과 연결 종료
처리로 명령이 끊기면 로봇을 정지합니다. 리프트 수동제어는 이 WebSocket 범위에 포함하지
않습니다.

[문서 목록](README.md) · [이전](02_architecture.md) · [다음](04_data_flow.md)
