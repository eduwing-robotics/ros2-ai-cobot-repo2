# Robot Control / FR5 · ZeKeep

조립을 수행하는 로봇 제어 영역입니다. FR5 협동로봇이 밑판과 벽체를 파지 · 삽입하고, ZeKeep 갠트리 로봇이 자재 적재와 조립 보조를 담당합니다.

---

## 프로젝트 정보

| 항목 | 내용 |
|:---|:---|
| 프로젝트 | AI 기반 조립식 주택 자동화 공장 |
| 형태 | 팀 프로젝트 |
| 담당 영역 | Robot Operation / FR5 · ZeKeep |
| 개발 환경 | 작성 예정 |
| 주요 기술 | 작성 예정 |
| 하드웨어 | FR5 협동로봇, ZeKeep 갠트리 로봇 2대 |

---

## 담당 범위

### FR5

- 부품 파지 및 조립 동작 제어
- 그리퍼 제어
- Vision 결과 기반 위치 보정
- 알람 · 오류 복구 처리

### ZeKeep

- 자재 적재 및 이송
- 흡착 제어
- 원점 복귀 및 자세 관리

세부 항목은 담당자가 확정 후 작성합니다.

---

## 시스템 연동

| 대상 | Interface |
|:---|:---|
| Team Server / FMS | 작성 예정 |
| AI Perception | 작성 예정 |
| Control Tower GUI | 작성 예정 |

---

## 저장소 구조

```text
robot_control_fr5_zekeep/
├─ README.md
├─ src/
├─ config/
└─ docs/
   └─ README.md
```

티칭 자세 값과 캘리브레이션 결과는 재현에 필요하므로 저장소에 포함합니다.

---

## 상세 문서

[문서 목록](docs/README.md)
