# 문서 목록

AI Perception / Vision 구현을 **Global Vision**과 **Depth Vision**으로 구분해 정리한 상세 기술 문서입니다.

| 번호 | 문서 | 내용 |
|:---:|:---|:---|
| 01 | [프로젝트 개요](01_overview.md) | 프로젝트 배경, 문제 정의, 개발 목적, Vision 시스템의 역할 |
| 02 | [시스템 아키텍처](02_architecture.md) | Global / Depth Vision 구조와 Server/FMS, Unity 연동 구조 |
| 03 | [주요 기능](03_features.md) | Global Camera, Incoming QA, PRE_ROOF 5-View 등 핵심 구현 |
| 04 | [데이터 흐름](04_data_flow.md) | Camera 입력부터 검사, 상태 처리, Result, Unity Video까지 Runtime 흐름 |
| 05 | [검증](05_validation.md) | 실제 장비, Actual E2E, Dummy E2E, Local Wire Test를 구분한 검증 결과 |
| 06 | [프로젝트 범위](06_project_scope.md) | 팀 전체 시스템과 직접 담당한 AI Perception 영역의 경계 |
| 07 | [프로젝트 구조](07_project_structure.md) | 공개 코드 구조, 포함 파일, 제외한 개발 산출물 |

---

## 문서 구성 원칙

- 팀 전체 프로젝트를 설명하되, **직접 담당한 AI Perception / Vision 영역을 중심으로 작성**합니다.
- Global Vision과 Depth Vision의 Runtime 구조와 검증 상태를 구분합니다.
- Team Server/FMS, Unity, Robot 내부 구현은 다른 담당 영역으로 분리하고, Vision 측 Interface와 연동 범위만 설명합니다.
- Dummy E2E, Local Wire E2E, Actual Server E2E, Actual Unity E2E, 실제 장비 E2E를 서로 다른 검증 단계로 기록합니다.
- 실제로 검증되지 않은 항목은 PASS로 표현하지 않습니다.
- 대용량 Raw / Synthetic Dataset, Model Weight, Runtime DB, Probe, Canary, Legacy 전체는 공개 저장소에 포함하지 않습니다.

---

## 미디어

대표 미디어는 실제 검증이 완료된 항목에 한해 관련 기능 또는 검증 문서에 직접 배치합니다.

[AI Perception README로 돌아가기](../README.md)
