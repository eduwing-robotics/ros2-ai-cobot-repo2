# 07. 프로젝트 구조

## 공개 코드 구조

```
robot_control_fr5_zekeep/
├── README.md
├── src/
│   ├── fr5_cycle/                 벽 삽입 파이프라인 · 비전 보정 (Python)
│   │   ├── house_cycle.py         단계 상태기계 · 게이트 · 웹 사이클 서버(:8776) · 집 타입 전환
│   │   ├── hover_align.py         정렬 높이 상대 정렬 — 기준 관계, 예측 이동 매칭, 노출 사다리, 같은 점 증명
│   │   ├── place_calc.py          랙 측정 · 파지 · 하강 밀림 감시 · 안착 판정 · 그리퍼 대기/409 재시도
│   │   ├── pillar_dots.py         색점 검출기 (HSV 범위 · 조각 병합 · 면적 하한)
│   │   ├── house_geometry.py      집 타입별 슬롯 · 벽 기하, 각도 wrap
│   │   ├── base_depth_corner.py   뎁스로 밑판 사각형 · 기둥 4점
│   │   ├── base_twist.py          ArUco 고정 자 — 카메라 복귀 오차
│   │   ├── side_seat.py           측면캠 안착 보조 판정
│   │   ├── slot_target.py         기둥 px → 로봇 → 슬롯 목표
│   │   ├── color_lock.py          색점 노출 · 화이트밸런스 고정 · 건강 게이트
│   │   ├── refpts_overlay.py      단계별 기준점 오버레이 데이터
│   │   ├── pillar_view.py / ref_view.py   기준점 오버레이 뷰 (:8773 / :8777)
│   │   ├── fork_carry.py          완성품 출하 리프트
│   │   └── mock_house_cycle.py    모의 브리지 사이클 시험
│   ├── bridge/bridge_server.py    FR5 명령 단일 창구 (:8765) — SDK, 동결 복구, J6 봉인, rescue
│   ├── orchestrator/
│   │   ├── cell_orchestrator.py   서버 계약 노드 — Action/Service/Topic, 상태기계, 오류코드
│   │   ├── fr5_real_adapter.py    실물 어댑터 (MoveIt2 · 그리퍼)
│   │   └── fr5_adapter_mock_test.py  모의 어댑터 시험
│   ├── cell_interfaces/           ROS 2 패키지 — ExecuteTask.action · CellControl.srv · GetPickPose.srv · PickCandidate.msg
│   ├── cameras/
│   │   ├── cam_server.py          손목 D435 / USB 카메라 MJPEG 서버 (노출 · WB · 뎁스 그리드 · 도트 오버레이)
│   │   ├── global_udp_cam.py      글로벌캠 중계 (:8779) — ROS 토픽 구독 또는 USB 직결
│   │   └── cam_autostart.sh       카메라 자동복구 데몬 (USB 탈락 · 정지 · 밝기 이상)
│   ├── console/bf2_robot_console.html   통합 운영 콘솔
│   └── zekeep/                    ZeKeep 3축 PLC 제어 도구 (zk_*.py, 9,194줄) · zkbot2_control/ 2호기 세팅 기록
├── scripts/
│   ├── fr5_up.sh · fr5_home.sh · fr5_rescue.sh    FR5 스택 기동 · 홈 · 동결 복구
│   ├── start_console.sh · start_cam.sh            카메라 · 콘솔 · 사이클 서버 기동
│   ├── start_factory_view.sh                      글로벌캠 → ROS 토픽 + Unity UDP (비전팀 스택)
│   ├── team_env.sh · telemetry_up.sh              팀 DDS 도메인 · 텔레메트리
│   └── move_all_three.sh                          FR5 + ZeKeep ×2 동시 구동 시험
├── config/                        티칭 · 캘리브레이션 결과 (재현에 필요하므로 포함)
│   ├── house_a/ · house_b/        slot_ref · hover_ref · rack_ref · pillar_pick · aruco_ref · grasp_sig · fork · rack_map · rack_pose · base_expo · side_seat_ref
│   ├── cam2robot_observe.json · cam2robot_rack.json · cam2robot_newcam.json   자세별 픽셀→mm 자코비안(실측)
│   ├── dot_calib.json · base_twist_ref.json · color_lock.json
│   └── cyclonedds_unicast.xml     DDS 유니캐스트 설정
└── docs/                          01 ~ 07
```

## 포함한 것

- 실기 최종본 코드 전부 (실험용 · 폐기된 스크립트는 제외)
- 집 타입별 기준 파일 — 이 값이 없으면 사이클을 재현할 수 없음
- 카메라→로봇 매핑 실측값
- 기동 · 복구 스크립트

## 제외한 것

- 로그(`*.log`, `nudge_log.jsonl`), 런타임 상태(`base_last`, `stage`), 백업 파일(`*.BEFORE_*`)
- 기준 촬영 이미지 · 캡처 (용량 · 재현 무관)
- FR5 · ZeKeep 제조사 자료(PDF · 래더 · 드라이버 문서)
- ROS 2 빌드 산출물(`build/`, `install/`), 벤더 패키지(`fairino_*`, `zkbot_description` — 팀 워크스페이스에서 별도 관리)
- 개발 중 실험 스크립트 (골든 정렬 · 앞끝우선 · 거리 정렬 등 폐기된 방식)

## 코드 규모

| 영역 | 파일 | 줄 |
|---|---:|---:|
| fr5_cycle | 15 | 8,191 |
| bridge | 1 | 2,855 |
| orchestrator | 3 | 2,139 |
| cameras · console | 4 | 1,745 + 1,383 (HTML) |
| zekeep | 63 | 9,194 |

## 실행 환경

| 항목 | 값 |
|---|---|
| OS / ROS | Ubuntu 24.04 / ROS 2 Jazzy, CycloneDDS |
| Python | 3.12 — OpenCV, NumPy, pyrealsense2, minimalmodbus, FastAPI/uvicorn |
| FR5 | Fairino FR5, Python SDK, 전용 유선 192.168.58.2 (100M 고정 협상) |
| ZeKeep | 3축 PLC 로봇 ×2, 미쓰비시 FX3U, MODBUS-RTU (USB 시리얼) |
| 카메라 | Intel RealSense D435 (손목), Logitech C270 (측면·글로벌), USB 카메라 (보조) |

---

[문서 목록으로](README.md)
