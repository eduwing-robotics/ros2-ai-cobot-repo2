# Forklift TurtleBot3 문서

| 번호 | 문서 | 내용 | 상태 |
|:---:|:---|:---|:---:|
| 01 | [프로젝트 개요](01_overview.md) | 목적, 운용 방식, 연동 대상 | 완료 |
| 02 | [시스템 아키텍처](02_architecture.md) | PC/Pi 노드 배치와 속도 명령 소유권 | 완료 |
| 03 | [주요 기능](03_features.md) | 운반, 도킹, 리프트, HOME, Unity | 완료 |
| 04 | [데이터 흐름](04_data_flow.md) | Action과 장치별 런타임 흐름 | 완료 |
| 05 | [검증](05_validation.md) | 모의시험과 실물 시나리오 결과 | 완료 |
| 06 | [프로젝트 범위](06_project_scope.md) | 담당 영역과 외부 시스템 경계 | 완료 |
| 07 | [프로젝트 구조](07_project_structure.md) | 공개 파일과 제외 산출물 | 완료 |

## 연동 상세

- [Main Server 연동 계약](main_server_integration.md)
- [Unity 직접 수동주행 WebSocket](unity_manual_websocket.md)
- [리프트 설치와 캘리브레이션](../hardware/lift/README.md)

## 문서 원칙

- 실제로 검증하지 않은 기능은 PASS로 표시하지 않습니다.
- 작업장 경로와 도킹 캘리브레이션은 소스와 함께 관리합니다.
- Pi의 런타임 리프트 위치 파일, 빌드 결과, 로그와 비밀정보는 커밋하지 않습니다.

[프로젝트 README로 돌아가기](../README.md)
