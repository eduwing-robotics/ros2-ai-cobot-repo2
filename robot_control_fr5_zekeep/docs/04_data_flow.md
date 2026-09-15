# 04. 데이터 흐름

## Runtime 한 사이클 (서버 지시 → 로봇 동작 → 상태 보고)

```
서버 ──ExecuteTask{zone, slot, part_code, req_id}──▶ cell_orchestrator
                                                     │  1) 명령 검증 (ver · req_id · 상태 전이 가능 여부)
                                                     │  2) task → phase 시퀀스 생성
                                                     │  3) phase 마다 adapter 호출, 완료 확인 후 다음 phase
                                                     │     (feedback: phase 이름 · 진행률)
                                                     ▼
                                              house_cycle (:8776)  ── 벽 한 장 = BASE→RACK→CARRY→ALIGN→DESCEND→SEAT
                                                     │  카메라 4대 측정 ↔ config/ 기준 비교 → 이동량 계산
                                                     ▼
                                              bridge_server (:8765) ── /fr5/move_tcp · /fr5/gripper (dry_run → 실행)
                                                     ▼
                                                   FR5
                                                     │  joint_states 30 Hz ──▶ 관제(Unity)
서버 ◀──/cell/status 1 Hz (cell_state · phase) ─── cell_orchestrator
서버 ◀──ExecuteTask result: SUCCESS | E-코드 ─────┘
```

## 입력

| 입력 | 출처 | 형식 |
|---|---|---|
| 작업 지시 | 서버 FMS | `ExecuteTask` goal: `zone`(WALL_EXT_PALLET / WALL_INT_PALLET …), `slot`, `part_code`, `req_id` |
| 운영 명령 | 서버 FMS | `CellControl {ver, cmd: PAUSE\|RESUME\|ABORT\|RESET, req_id}` |
| 집 타입 · 색별 기준 | `config/house_a`, `config/house_b` | `slot_ref`(안착 TCP·밑판 자세), `hover_ref`(정렬 기준 관계), `rack_ref`(랙 파지 기준), `pillar_pick`(지정 기둥), `aruco_ref`, `grasp_sig`, `fork` |
| 카메라→로봇 매핑 | `config/cam2robot_*.json`, `dot_calib.json` | 자세별 `Jinv_mm_per_px` (실측) |
| 카메라 프레임 | :8766 / :8768 / :8771 / :8779 | MJPEG `/raw`, 뎁스 `/depthgrid`, 노출 `/expo` |

## 처리 — 벽 한 장

| 단계 | 측정 | 계산 | 출력(명령) |
|---|---|---|---|
| BASE | 기둥 색점 4점 + ArUco 4마커 (손목캠, 관측자세) | 밑판 x·y·yaw, 카메라 복귀 오차 보정 → 색별 슬롯 목표 = `slot_ref` 를 강체 변환 | `base_last.json` 갱신 |
| RACK | 벽 색점 양끝·가운데 (손목캠, 랙 관측자세), 길이 ±2 % 통과분 중앙값 | 파지점 XY · 각 → 파지 TCP | 파지 XY 위 → −40 → 파지 자세 → 닫기 → 들어올림 |
| CARRY | — | 슬롯 목표 + 밑판 이동분 | SAFE(z650) → 목표 위 → 정렬 높이 |
| ALIGN | 지정 기둥 1점 + 든 벽 점 1~2점 (손목캠 조종, 보조캠 참고) | Δpx = (벽−기준벽) − (기둥−기준기둥) → `Jinv`·Δpx = Δmm (+ 2점이면 rz) | 상대 이동 (≤3 mm 걸음), 수렴 시 WAIT DESCEND |
| DESCEND | 든 벽 점 픽셀 (매 3 mm) | 단계·누적 밀림 | 하강 계속 / 정지·후퇴 |
| SEAT | 기둥 점 2개 | 안착 기준 자리와 일치(허용 12 px) | 개방 → 상승 → DONE |

## 상태 관리

- 셀 상태(`cell_state`): IDLE → RUNNING → (PAUSED) → IDLE / ERROR. 전이는 팀 상태 다이어그램의 조건(명령 유효성 · 공정 완료 · 로봇 오류 · 관리자 정지)만 사용
- 사이클 상태(`house_cycle` S): stage(1 BASE … WAIT DESCEND … DONE / STOPPED), busy, color, align(정렬 완료 TCP), err
- 일시정지는 phase 경계에서 멈추고 같은 phase 부터 재개. 오류는 원인 조치 + RESET 뒤 해당 공정부터 재실행
- 성공한 삽입은 기준 승격 — 단, 지정 기둥 · 환산자 · 노출 등 설정 키는 보존

## 결과 전달

| 출력 | 대상 | 내용 |
|---|---|---|
| `ExecuteTask` result | 서버 | `SUCCESS` 또는 E-코드 (E204 로봇 오류, E301 파라미터, E505 비전 미응답 등) |
| `/cell/status` | 서버 · 관제 | 1 Hz, 현재 상태 · phase |
| `/fr5/joint_states` | 관제 | 30 Hz 관절각 → Unity 디지털 트윈 |
| 글로벌캠 | 관제 | C270 → factory_view 퍼블리셔 → UDP 21030 (Unity), ROS 토픽(도메인 90) |
| 사이클 로그 | 운영자 | `house_cycle.log` — 단계 · 측정치 · 게이트 판정 · 정지 사유 |

## 오류 · 정지 경로

| 상황 | 동작 |
|---|---|
| 게이트 정지 (검출 · 길이 · 정렬 상한) | 로봇 그 자리 정지, 사유를 로그와 콘솔에 표시, 부품은 사람이 처리 |
| 하강 중 밀림 감지 | 정지 → 25 mm 후퇴 → 사람 확인 |
| 명령 충돌 409 | 앞 명령 완료 대기 후 1회 재시도 |
| 컨트롤러 동결 | `fr5_rescue.sh`: 전원 재투입 → 펜던트 Clear(사람) → 링크 복구 → `/rescue/fr5` |
| 카메라 탈락 · 프레임 정지 | `cam_autostart.sh` 가 감지 · 재기동 · WB/노출 복원 |

---

[문서 목록으로](README.md)
