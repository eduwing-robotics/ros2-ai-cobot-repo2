# AI Perception / Vision 문서

`ai_perception`의 상세 기술 문서를 기능과 시스템 흐름 기준으로 정리합니다.

## 문서 목록

| 번호 | 문서 | 내용 |
|:---:|:---|:---|
| 01 | [프로젝트 개요](01_project_overview.md) | Vision 역할, 3개 카메라 구성, 검사 범위 |
| 02 | [시스템 아키텍처](02_system_architecture.md) | Incoming, Factory View, PRE_ROOF와 Server / Robot / Unity 연결 구조 |
| 03 | [입고 자재 검사](03_incoming_inspection.md) | RTSP, Auto Alignment, YOLO / QA, Server / Unity 연동 |
| 04 | [Factory View](04_factory_view.md) | Logitech C270, FFmpeg 보정, ROS2, Unity 영상 전송 |
| 05 | [PRE_ROOF 조립 품질검사](05_pre_roof_qc.md) | D435 RGB / Depth, 5방향 검사, 재검사 흐름 |
| 06 | [서버·로봇·Unity 연동](06_integration.md) | UDP Request / Result, Robot Control, HMV1 영상 인터페이스 |
| 07 | [검증 결과](07_validation.md) | 실물 검사, Runtime, 통신, 영상 전송 검증 |
| 08 | [문제 해결 과정](08_problem_solving.md) | 데이터, 카메라, 검사 로직, 시스템 연동 문제와 개선 과정 |
| 09 | [프로젝트 구조](09_project_structure.md) | 코드·설정·문서 구성과 공개 저장소 포함 범위 |

---

## 문서 기준

- 최종 시스템 역할은 **Incoming Inspection Camera / Factory View Camera / RealSense D435**의 3개 카메라로 구분합니다.
- `global_camera`와 같은 기존 코드·Topic 이름은 구현 계보상 유지될 수 있으며, 문서에서는 현재 역할과 구분해 설명합니다.
- Team Server/FMS, Robot Control, Unity 내부 구현은 각 담당 시스템의 범위로 두고 Vision 측 Interface와 연동 지점을 중심으로 기록합니다.
- 검증 결과는 실물 검사, Runtime, 통신, 영상 전송 등 확인 범위를 구분해 표시합니다.
- 대용량 Dataset, Model Weight, Runtime DB, Probe / Canary / Backup 등 개발 산출물 전체는 저장소에 포함하지 않습니다.
- Runtime 버전과 설정값은 최종 운영 코드 및 Config와 대조해 유지합니다.

---

## 주요 영역

```text
AI Perception / Vision
├── Incoming Inspection
│   ├── RTSP / FFmpeg / ROS2
│   ├── Auto Alignment
│   ├── YOLO / QA
│   └── Server / Unity
│
├── Factory View
│   ├── Logitech C270
│   ├── FFmpeg View Correction
│   ├── ROS2 Image
│   └── Unity HMV1
│
└── PRE_ROOF
    ├── RealSense D435
    ├── TOP / LEFT / RIGHT / FRONT / BEHIND
    ├── Dashboard / Gateway
    └── Server / Robot / Unity
```

---

[AI Perception / Vision README로 돌아가기](../README.md)

---

## 전체 문서 목차

1. [AI Perception / Vision](../README.md)
2. [프로젝트 개요](01_project_overview.md)
3. [시스템 아키텍처](02_system_architecture.md)
4. [입고 자재 검사](03_incoming_inspection.md)
5. [Factory View](04_factory_view.md)
6. [PRE_ROOF 조립 품질검사](05_pre_roof_qc.md)
7. [서버·로봇·Unity 연동](06_integration.md)
8. [검증 결과](07_validation.md)
9. [문제 해결 과정](08_problem_solving.md)
10. [프로젝트 구조](09_project_structure.md)
