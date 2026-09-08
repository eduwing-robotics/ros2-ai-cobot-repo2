# Forklift TurtleBot3

TurtleBot3 기반 자재 운반 로봇입니다. 자율주행으로 자재 랙과 조립 구역을 오가며, 스테퍼 모터 포크 리프트로 자재를 적재 · 하역합니다.

---

## 프로젝트 정보

| 항목 | 내용 |
|:---|:---|
| 프로젝트 | AI 기반 조립식 주택 자동화 공장 |
| 형태 | 팀 프로젝트 |
| 담당 영역 | Logistics / TurtleBot3 Forklift |
| 개발 환경 | 작성 예정 |
| 주요 기술 | 작성 예정 |
| 하드웨어 | TurtleBot3, Raspberry Pi, 28BYJ-48 스테퍼 포크 리프트 |

---

## 담당 범위

- SLAM · Nav2 기반 자재 운반 주행
- 자재 랙 / 조립 구역 도킹
- 포크 리프트 승강 제어
- 상위 시스템 작업 지시 연동

세부 항목은 담당자가 확정 후 작성합니다.

---

## 시스템 연동

| 대상 | Interface |
|:---|:---|
| Team Server / FMS | 작성 예정 |
| Control Tower GUI | 작성 예정 |
| 리프트 제어 | 작성 예정 |

---

## 저장소 구조

```text
forklift_turtlebot3/
├─ README.md
├─ src/
├─ config/
└─ docs/
   └─ README.md
```

맵, 순찰 · 운반 경로, 캘리브레이션 결과는 재현에 필요하므로 저장소에 포함합니다.

---

## 상세 문서

[문서 목록](docs/README.md)
