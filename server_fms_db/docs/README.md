# Server / FMS / DB 문서

이 디렉터리는 [`server_fms_db`](../README.md)의 상세 기술 문서입니다. 모듈 개요와 실행 방법은 상위 README를 참고하세요.

| 문서 | 내용 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | API Server, FMS Worker, Telemetry Gateway, PostgreSQL, Redis의 구조와 데이터 흐름 |
| [02_orchestration_lifecycle.md](02_orchestration_lifecycle.md) | Production Job, JobStep, ExecutionAttempt, Material Delivery, Inspection과 FMS orchestration |
| [03_interfaces.md](03_interfaces.md) | Unity, Robot Cell, TurtleBot, Vision 연동 contract |
| [04_troubleshooting_validation.md](04_troubleshooting_validation.md) | 통합 과정의 문제 해결 사례와 검증 범위 |

## 문서 구성 원칙

- 현재 source와 test로 확인되는 사실만 문서화합니다.
- 실제 장비 동작을 검증하지 않은 경우 PASS로 표현하지 않습니다.
- 타 팀의 내부 구현보다 Server/FMS가 사용하는 interface와 책임 경계를 중심으로 설명합니다.
- PostgreSQL durable state와 Redis realtime state/event의 역할을 구분합니다.
- 운영 비밀값, DB dump, runtime artifact는 공개 문서와 저장소에 포함하지 않습니다.
