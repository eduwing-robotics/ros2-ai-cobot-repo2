# 문서 목록

Robot Control / FR5 · ZeKeep 영역의 상세 기술 문서입니다.

| 번호 | 문서 | 내용 | 상태 |
|:---:|:---|:---|:---:|
| 01 | [프로젝트 개요](01_overview.md) | 배경, 문제 정의, 개발 목적, 담당 영역의 역할 | 작성 완료 |
| 02 | [시스템 아키텍처](02_architecture.md) | 3계층 구조와 타 영역 연동 구조 | 작성 완료 |
| 03 | [주요 기능](03_features.md) | 벽 삽입 파이프라인 · 비전 보정 · ZeKeep PLC 제어 · 안전 게이트 | 작성 완료 |
| 04 | [데이터 흐름](04_data_flow.md) | 서버 지시부터 로봇 동작, 상태 보고까지 Runtime 흐름 | 작성 완료 |
| 05 | [검증](05_verification.md) | Dummy · Local · Actual 단계별 결과와 정지 기록 | 작성 완료 |
| 06 | [프로젝트 범위](06_project_scope.md) | 팀 전체 시스템과 담당 영역의 경계 | 작성 완료 |
| 07 | [프로젝트 구조](07_project_structure.md) | 공개 코드 구조, 포함 파일, 제외한 산출물 | 작성 완료 |
| 부록 | [ZeKeep 2호기 실측 세팅 기록](zekeep_unit2_setup_record_20260808.md) · [흡착 밸브 시험 기록](zekeep_unit2_suction_valve_test_20260808.md) | 2호기 개체차 · 밸브 시험 원문 | 원문 |

---

## 문서 구성 원칙

- 팀 전체 프로젝트를 설명하되, 담당 영역을 중심으로 작성합니다.
- 타 담당 영역의 내부 구현은 분리하고, 연동 Interface와 범위만 설명합니다.
- Dummy, Local, Actual 검증을 서로 다른 단계로 기록합니다.
- 실제로 검증되지 않은 항목은 PASS로 표현하지 않습니다.
- 대용량 Dataset, Model Weight, Runtime DB, 로그, 백업 파일은 저장소에 포함하지 않습니다.

---

[Robot Control / FR5 · ZeKeep README로 돌아가기](../README.md)
