# 문서 목록

Voice AI 영역의 상세 기술 문서입니다. 전체 기능과 발화 예시는 [상위 README](../README.md)를 참고하세요.

| 문서 | 내용 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | Voice Runtime, API Server, FMS 연동 구조와 데이터 흐름 |
| [02_troubleshooting_validation.md](02_troubleshooting_validation.md) | Wake Word · STT · 명령 해석 · TTS의 문제 해결 및 검증 |
| [03_project_structure.md](03_project_structure.md) | 실제 source 배치, 모듈 경계, 공개·제외 범위 |

## 문서 구성 원칙

- 실제 코드와 테스트로 확인되지 않은 항목을 PASS 또는 성능 수치로 표현하지 않습니다.
- 대용량 원본 Dataset·Model Weight·Runtime DB·백업 산출물은 저장소에 포함하지 않습니다.
- 타 담당 영역의 내부 구현을 반복하기보다 Voice AI가 사용하는 interface와 책임 경계를 중심으로 설명합니다.
