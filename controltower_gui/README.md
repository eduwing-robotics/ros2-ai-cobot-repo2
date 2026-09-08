# Control Tower GUI

조립식 주택 자동화 공정의 전체 상태를 한 화면에서 관제하는 GUI입니다. 공정 진행, 로봇 상태, Vision 검사 결과, 이벤트 이력을 표시하고 관리자 수동 제어를 제공합니다.

---

## 프로젝트 정보

| 항목 | 내용 |
|:---|:---|
| 프로젝트 | AI 기반 조립식 주택 자동화 공장 |
| 형태 | 팀 프로젝트 |
| 담당 영역 | Control Tower GUI |
| 개발 환경 | 작성 예정 |
| 주요 기술 | 작성 예정 |

---

## 담당 범위

- 공정 진행 상태 표시
- 로봇 상태 모니터링 (FR5 / ZeKeep / TurtleBot)
- Vision 검사 결과 표시
- 이벤트 · 알람 이력 조회
- 관리자 수동 제어 및 오류 RESET

세부 항목은 담당자가 확정 후 작성합니다.

---

## 시스템 연동

| 대상 | Interface |
|:---|:---|
| Team Server / FMS | 작성 예정 |
| AI Perception | 작성 예정 |
| Robot Control | 작성 예정 |

공정 상태의 Source of Truth는 Team Server/FMS가 담당하며, GUI는 표시와 명령 전달을 담당합니다.

---

## 저장소 구조

```text
controltower_gui/
├─ README.md
├─ src/
├─ config/
└─ docs/
   └─ README.md
```

---

## 상세 문서

[문서 목록](docs/README.md)
