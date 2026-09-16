# 인터페이스와 실행 설정

## 실행 순서

1. 팀 서버의 `/ws/unity`가 동작하는지 확인합니다.
2. Unity Hub에서 `controltower_gui` 폴더를 열고 SampleScene을 로드합니다.
3. 씬의 MainServerWebSocketClient `serverUrl`을 서버 주소에 맞춥니다. 현장 씬은 `ws://192.168.20.20:8000/ws/unity`입니다. 소스에 별도 WebSocketManager도 있으므로 활성 오브젝트와 연결 컴포넌트를 기준으로 확인합니다.
4. Vision 송신 목적지를 Unity PC IP로 설정하고 UDP 21010·21020·21030의 수신을 허용합니다.
5. Play 후 Console의 연결·수신 메시지와 각 화면을 확인합니다.

## 주요 메시지

| 메시지 / 필드 | Unity 처리 |
| --- | --- |
| `production_snapshot`, `production_status` | 작업·공정 상태 갱신 |
| `process_stage_code/order/display_name` | PROCESS 표시 기준 |
| `current_stage_code` | 기존 세부 단계 호환 |
| `robot_joint_state` | robot_id와 joint_names로 관절 매핑, positions는 radians |
| `robot_status` | 연결·준비·작업과 그리퍼 상태 |

FR5 `grip`은 0~100의 개방률로 해석합니다. 100은 최대 개방, 0은 닫힌 간격이며 양쪽 손가락을 비례 이동합니다. 초기 손가락 간격을 최대 개방 기준으로 저장합니다. 코드 기본값은 닫힌 상태에도 초기 간격의 12%를 남깁니다. `grip_real`은 진단 데이터로 보관하며 표시에는 사용하지 않습니다.

표시와 별개로 현재 코드는 개방률 30% 이하를 닫힘, 55% 이상을 열림으로 판정하고 중간은 이전 상태를 유지합니다. 이 판정은 파지 요청에도 연결되므로, 중간 개방폭에서 실제로 자재를 잡는 경우 파지 성공 이벤트와 구분해 검증해야 합니다. `grip`이 없으면 일부 current_operation 문자열로 개폐를 추정합니다.

## UDP 영상

| 용도 | 포트 | Stream ID |
| --- | --- | --- |
| 수입검사 | 21010 | 1 |
| 조립검사 | 21020 | 2 |
| 글로벌 카메라 | 21030 | 3 |

일반 Raw JPEG/H.264 스트림이 아니라 HMV1 헤더와 JPEG 조각 형식이 필요합니다. 검사 성공 메시지를 받았어도 영상 경로가 끊기면 화면은 대기 상태일 수 있습니다. 목적지 IP, 포트, Stream ID, 헤더와 프레임 조각을 각각 확인합니다.

## 12단계 PROCESS

1. COMMAND_RECEIVED — 작업 명령 전달
2. INCOMING_QA — 수입검사
3. BASE_INSTALL — Zekeep 베이스 설치
4. OUTER_WALL_DELIVERY — 외벽 팔레트 운반
5. OUTER_WALL_INSTALL — FR5 외벽 설치
6. OUTER_RETURN_INNER_DELIVERY — 외벽 빈 팔레트 회수 / 내벽 팔레트 운반
7. INNER_WALL_INSTALL — FR5 내벽 설치
8. INNER_WALL_RETURN — 내벽 빈 팔레트 회수
9. PRE_ROOF_INSPECTION — 조립 결과 검사
10. ROOF_INSTALL — Zekeep 지붕 설치
11. HOUSE_OUTBOUND — FR5 완성 주택 운반
12. COMPLETED — 작업 완료

실패·취소의 process_stage 필드는 null일 수 있으므로 Job 상태와 함께 해석합니다. 공정 완료 표시만으로 실제 로봇의 모든 동작이 검증됐다고 판단하지 않습니다.
