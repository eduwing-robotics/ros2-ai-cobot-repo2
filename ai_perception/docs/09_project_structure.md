# 프로젝트 구조

> **AI Perception / Vision 공개 저장소의 문서·코드·설정 구성과 공개 범위**
> `ai_perception` 디렉터리의 역할, 파일 배치 기준, 공개 저장소 포함·제외 범위를 정리했습니다.

---

## 1. 저장소 구성 원칙

`ai_perception` 영역은 단순 소스 코드 모음이 아니라 다음 네 가지 목적을 동시에 만족하도록 구성합니다.

1. **프로젝트 흐름을 빠르게 파악할 수 있어야 함**
2. **실제 구현 코드와 문서가 같은 기능 단위로 정리돼야 함**
3. **대용량 Dataset / Model / Runtime DB는 GitHub에서 제외해야 함**
4. **실제 최종 검증 이미지와 영상은 문서 안에서 바로 확인할 수 있어야 함**

따라서 저장소를 다음 네 영역으로 구분합니다.

```text
ai_perception/
├── README.md
├── config/
├── scripts/
└── docs/
```

---

## 2. 최상위 구조

최종 공개 구조는 다음 방향으로 정리합니다.

```text
ai_perception/
├── README.md
├── .gitignore
│
├── config/
│   ├── global_vision/
│   └── factory_view/
│
├── configs/
│   ├── harmony_unified_inspection_recipe_v2.json
│   └── harmony_unified_model_contract_v1.json
│
├── scripts/
│   ├── global_vision/
│   │   └── global_camera_ros2/
│   ├── tools/
│   ├── incoming_qa_runtime/
│   ├── pre_roof_runtime/
│   ├── network/
│   └── harmony_pre_roof_qc_*_runtime_*.py
│
├── datasets/
│   └── Runtime에 필요한 선별 Reference / Contract
│
└── docs/
    ├── README.md
    ├── 01_project_overview.md
    ├── 02_system_architecture.md
    ├── 03_incoming_inspection.md
    ├── 04_factory_view.md
    ├── 05_pre_roof_qc.md
    ├── 06_integration.md
    ├── 07_validation.md
    ├── 08_problem_solving.md
    ├── 09_project_structure.md
    │
    ├── assets/
    │   └── final/
    │       ├── incoming/
    │       ├── factory_view/
    │       └── pre_roof/
    │
    └── videos/
        └── incoming_inspection/
```

> 실제 최종 Runtime 파일명은 Ubuntu의 최종 운영본을 Windows 저장소로 동기화한 뒤 다시 한 번 감사해 확정합니다.
> 문서에서는 기능 역할을 먼저 고정하고, 코드 파일명은 최종 동기화된 실제 파일을 기준으로 맞춥니다.

---

## 3. README 역할

```text
ai_perception/README.md
```

README는 상세 기술 문서가 아니라 **프로젝트 전체를 빠르게 이해하기 위한 문서 진입점**입니다.

README에서 바로 확인할 수 있는 내용:

- Vision 파트 한 줄 요약
- 3개 Camera 역할
- 주요 기술
- Incoming Inspection
- Factory View
- PRE_ROOF 5방향 QC
- 실제 정상 / 불량 이미지
- 실제 검증 영상 6개
- 문제 해결 과정 요약
- 검증 결과
- 전체 상세 문서 목차

세부 구현 내용은 `docs/`로 분리합니다.

---

## 4. 상세 문서 구조

상세 문서는 기능이 아니라 **읽는 흐름**을 기준으로 구성합니다.

```text
01_project_overview.md
→ 프로젝트에서 Vision이 무엇을 담당했는가

02_system_architecture.md
→ 시스템이 어떻게 연결되는가

03_incoming_inspection.md
→ 입고 자재 검사를 어떻게 구현했는가

04_factory_view.md
→ 공장 전체 실영상을 어떻게 구성했는가

05_pre_roof_qc.md
→ D435 5방향 품질검사를 어떻게 구현했는가

06_integration.md
→ Server / Robot / Unity와 어떻게 연동했는가

07_validation.md
→ 무엇을 어떤 근거로 검증했는가

08_problem_solving.md
→ 실제 문제를 어떻게 분석하고 해결했는가

09_project_structure.md
→ 저장소를 어떤 기준으로 정리했는가
```

모든 상세 문서 맨 아래에는 동일한 **전체 문서 목차**를 배치해 다른 문서로 바로 이동할 수 있도록 구성합니다.

---

## 5. `docs/README.md` 역할

`docs/README.md`는 상세 문서 전용 인덱스입니다.

README와 역할이 겹치지 않도록 다음 정도만 유지합니다.

```text
AI Perception 상세 문서

1. 프로젝트 개요
2. 시스템 아키텍처
3. 입고 자재 검사
4. Factory View
5. PRE_ROOF 조립 품질검사
6. 서버·로봇·Unity 연동
7. 검증 결과
8. 문제 해결 과정
9. 프로젝트 구조
```

최종 정리 단계에서 기존 `docs/README.md`도 이 구조에 맞게 교체합니다.

---

## 6. Config 구조

`config/`에는 Runtime에서 사용하는 **공개 가능한 설정과 Contract**를 배치합니다.

예시:

```text
config/
├── global_vision/
│   ├── model_contract
│   ├── inspection_recipe
│   └── runtime_config
│
├── factory_view/
│   └── factory_view_final_config
│
└── pre_roof/
    ├── view_config
    ├── threshold_config
    └── integration_config
```

Config와 코드의 역할은 분리합니다.

```text
Python
= Runtime Logic

JSON / YAML
= Threshold / Contract / Camera / Runtime Setting
```

Threshold와 Camera Setting을 코드에 무조건 Hard Coding하지 않고, 반복 조정이 필요한 값은 Config로 분리하는 방향을 유지합니다.

---

## 7. Incoming Inspection 코드 구조

Incoming Inspection 관련 공개 코드는 다음 역할 단위로 정리합니다.

```text
scripts/global_vision/
├── camera/
├── inspection/
├── alignment/
├── runtime/
└── network/
```

대표 역할:

### Camera

- RTSP 수신
- FFmpeg Direct Pipe
- ROS2 Image Publisher

### Alignment

- Fixed Structure Anchor
- ECC Alignment
- Alignment Gate

### Inspection

- YOLO Inference
- HOUSE A / HOUSE B 검사
- ROI / Slot 판정

### Runtime

- Incoming QA
- Transaction 관리
- Annotated Image

### Network

- Team Server UDP
- Unity HMV1 Video

최종 GitHub에서는 실제 코드 파일명을 유지하되, 문서에서는 위 기능 그룹을 기준으로 설명합니다.

---

## 8. PRE_ROOF 코드 구조

PRE_ROOF 관련 공개 코드는 다음 역할로 나눕니다.

```text
scripts/
├── harmony_pre_roof_qc_top_runtime_v7.py
├── harmony_pre_roof_qc_left_runtime_v4.py
├── harmony_pre_roof_qc_right_runtime_v8.py
├── harmony_pre_roof_qc_front_runtime_v2.py
├── harmony_pre_roof_qc_behind_runtime_v5.py
├── harmony_pre_roof_qc_5view_dashboard_v17.py
├── pre_roof_runtime/
│   ├── udp_gateway_v4.py
│   ├── d435_raw_mjpeg_bridge_v3.py
│   └── pre_roof_active_view_annotated_publisher_v3.py
└── network/
    └── harmony_unity_video_udp_v3.py
```

최종 운영 계보의 핵심 구성 요소:

```text
TOP Runtime
LEFT Runtime
RIGHT Runtime
FRONT Runtime
BEHIND Runtime
5-View Dashboard
UDP Gateway
D435 Raw MJPEG Bridge
Active View Annotated Publisher
Unity Video Sender
```

최종 운영 기준 버전:

| 구성 요소 | 최종 기준 |
|:---|:---:|
| TOP | V7 |
| LEFT | V4 |
| RIGHT | V8 |
| FRONT | V2 |
| BEHIND | V5 |
| Dashboard | V17 |
| Gateway | V4 |
| D435 Raw MJPEG Bridge | V3 |
| Active View Annotated Publisher | V3 |
| Unity Video Sender | V3 |

> Ubuntu 최종 운영본을 현재 저장소에 동기화했으며, 위 버전 기준으로 Runtime과 문서의 일치 여부를 확인했습니다.

---

## 9. Factory View 코드 구조

Factory View는 다음 요소로 구성합니다.

```text
Factory View Publisher
Factory View Config
Unity Video Sender
Start / Stop / Verify Script
```

최종 운영 역할:

```text
Logitech C270
→ FFmpeg
→ ROS2
→ Unity
```

Robot Control PC 인계 Archive:

```text
factory_view_robot_control_v1.tar.gz
```

Factory View는 최종 운영에서 Vision PC에 종속되지 않도록 실행 구성을 독립화했습니다.

---

## 10. 최종 검증 이미지 구조

GitHub에는 대용량 Dataset 대신 **최종 검증 결과를 보여주는 실제 캡처 이미지**를 포함합니다.

```text
docs/assets/final/
├── incoming/
│   ├── house_a_pass.jpg
│   ├── house_a_fail.jpg
│   ├── house_b_pass.jpg
│   └── house_b_fail.jpg
│
├── factory_view/
│   └── factory_view_overview.png
│
└── pre_roof/
    ├── pre_roof_final_pass.png
    └── pre_roof_fail.png
```

총 7장의 대표 이미지를 README와 상세 문서의 관련 설명 위치에 직접 표시합니다.

---

## 11. 영상 관리

최종 문서에서 사용하는 실제 검증 영상은 총 6개입니다.

```text
HOUSE A 정상
HOUSE A 불량
HOUSE B 정상
HOUSE B 불량
PRE_ROOF 정상
PRE_ROOF 불량
```

### Incoming 영상

기존 Incoming MP4 4개는 이미 Git에서 추적 중이므로 그대로 유지합니다.

```text
docs/videos/incoming_inspection/
```

### PRE_ROOF 영상

PRE_ROOF 최종 영상 2개는 GitHub `user-attachments`를 사용합니다.

이유:

- 루트 `.gitignore`에서 신규 `*.mp4`가 제외됨
- 저장소에 대용량 MP4를 중복 저장할 필요가 없음
- GitHub README / Markdown에서 실제 영상 Player로 바로 확인 가능

따라서 문서에서는 영상 파일 폴더를 찾게 하지 않고 **관련 설명 바로 아래에 영상 링크를 배치**합니다.

---

## 12. GitHub에 포함하는 항목

다음 항목은 구현 내용과 설정 확인에 필요하므로 공개 저장소에 포함합니다.

### 코드

- Camera Input Runtime
- Auto Alignment
- Incoming Inspection Runtime
- PRE_ROOF View Runtime
- Dashboard
- Server / UDP Interface
- Unity Video Sender
- Runtime Contract

### Config

- 공개 가능한 Model Contract
- Inspection Recipe
- Camera Config
- Runtime Config
- Threshold Config

### 문서

- README
- 상세 문서
- System Diagram
- Validation 기록
- Problem Solving 기록

### 검증 자료

- 최종 실물 캡처
- GitHub 영상 링크
- 공개 가능한 검증 결과

---

## 13. GitHub에서 제외하는 항목

다음 항목은 저장소 용량, 운영 데이터, 중복 Artifact 문제 때문에 공개 GitHub에서 제외합니다.

### 대용량 Dataset

```text
Raw Image Dataset
Full Synthetic Dataset
Training Dataset
Augmented Dataset
Physical Capture Pool
```

### Model Weight

```text
*.pt
*.pth
```

### Runtime Database

```text
*.sqlite
*.sqlite3
```

### 환경 정보

```text
.env
.env.*
```

### 개발 중간 산출물

```text
Backup
Probe
Canary Output
Temporary Capture
Contact Sheet
Debug Artifact
```

대표 `.gitignore` 정책:

```text
__pycache__/
*.py[cod]
*.pt
*.pth
*.sqlite
*.sqlite3
.env
.env.*
docs/assets/_capture_candidates/
docs/assets/_contact_sheets/
```

루트 저장소의 공통 `.gitignore` 정책도 함께 적용됩니다.

---

## 14. 왜 Model Weight를 제외하는가

공개 저장소의 목적은 대용량 Weight를 배포하는 것이 아니라 다음을 보여주는 것입니다.

```text
문제를 어떻게 정의했는가
→ 데이터를 어떻게 만들었는가
→ 모델을 어떻게 검증했는가
→ 실제 카메라에서 왜 실패했는가
→ 어떻게 보정했는가
→ 시스템에 어떻게 연결했는가
```

따라서 GitHub에는 Model Weight 자체보다 다음을 우선합니다.

- Model Contract
- Class Definition
- Training / Runtime Logic
- Validation 결과
- Problem Solving 과정

---

## 15. 왜 전체 Dataset을 제외하는가

Synthetic / Physical Dataset 전체는 수만 장 규모로 저장소에 적합하지 않습니다.

대신 GitHub에서는 다음 정보를 문서화합니다.

- 데이터 생성 방식
- 클래스 구조
- Camera Geometry
- 거리 / 각도 / 조명 정책
- Synthetic → Real 개선 과정
- 대표 검증 결과

이를 통해 Repository는 가볍게 유지하면서도 **어떤 방식으로 데이터를 설계했는지 설명 가능**하도록 구성합니다.

---

## 16. 중간 실험 자료 관리

개발 중에는 많은 실험 자료가 생성됐습니다.

예:

```text
Canary
Probe
Attempt
Backup
Comparison Image
Contact Sheet
Temporary Capture
```

이 파일들은 개발 이력에는 중요하지만 최종 최종 문서에 모두 포함하면 핵심 흐름이 흐려집니다.

따라서 최종 GitHub에는 다음 원칙을 적용합니다.

```text
최종 결과 설명에 필요한가?
    Yes → 정리해서 포함
    No  → 로컬 보존 / Git 제외
```

---

## 17. 코드 공개 기준

코드는 양보다 **최종 구현을 설명하는 코드가 실제 검증본과 일치하는지**를 우선합니다.

공개 기준:

```text
최종 Runtime인가?
실제 검증된 계보인가?
문서에서 설명하는 기능과 일치하는가?
필요한 Config가 함께 있는가?
비밀 정보가 없는가?
```

하나라도 충족하지 않으면 최종 공개 전에 다시 확인합니다.

---

## 18. 최종 코드 동기화 기준

최종 Vision Runtime은 실제 운영에 사용한 Ubuntu 환경을 기준으로 확인한 뒤 현재 저장소에 동기화했습니다.

동기화 과정에서는 다음 항목을 확인했습니다.

```text
Ubuntu 최종 운영본 확인
→ 최종 Runtime / Config / Reference 선별
→ 기존 구버전 Runtime 제거
→ 최종 Runtime 경로 유지
→ 필요한 Config / Reference 복사
→ Python Syntax 및 SHA256 검증
→ 문서의 Version / Topic / Port 대조
```

대용량 학습 원본과 Model Weight는 저장소에서 제외하고, 실행 구조를 확인하는 데 필요한 코드·계약·설정·Reference와 대표 이미지만 포함합니다.

---

## 19. 오래된 코드 처리 원칙

Git History에는 이전 버전이 남을 수 있지만 최종 `ai_perception` 디렉터리에서는 이 혼동하지 않도록 정리합니다.

원칙:

```text
최종본과 역할이 중복되는 오래된 Runtime
→ 최종 동기화 후 정리 검토

다른 기능 / History 설명에 필요한 코드
→ 보존

최종 기능 설명과 충돌하는 코드
→ 삭제 또는 archive 여부 검토
```

단, 기존 파일은 무작정 삭제하지 않고 **최종 Runtime과 역할이 완전히 중복되는지 확인한 뒤** 정리합니다.

---

## 20. 문서 구조 전환

상세 문서는 현재 시스템 역할과 최종 구현 흐름을 기준으로 01~09 구조로 통합했습니다.

```text
README.md
→ 전체 Vision 구성과 주요 결과

docs/README.md
→ 상세 문서 인덱스

01_project_overview.md
→ 프로젝트 개요

02_system_architecture.md
→ 시스템 아키텍처

03_incoming_inspection.md
→ 입고 자재 검사

04_factory_view.md
→ Factory View

05_pre_roof_qc.md
→ PRE_ROOF 조립 품질검사

06_integration.md
→ 서버·로봇·Unity 연동

07_validation.md
→ 검증 결과

08_problem_solving.md
→ 문제 해결 과정

09_project_structure.md
→ 프로젝트 구조
```

이전 문서 구조는 현재 작업 트리에서 제거했으며, 필요한 과거 기록은 Git History에서 확인할 수 있습니다.

현재 문서 기준:

```text
README.md
+
docs/README.md
+
docs/01~09
```

Runtime 버전, Topic, Port, Config 등 구현 세부사항은 실제 최종 운영 코드와 대조해 유지합니다.

---
## 21. 최종 저장소 구조

Repository는 다음 흐름으로 확인할 수 있게 구성합니다.

```text
ai_perception/README.md
        ↓
실제 이미지 / 실제 영상
        ↓
전체 시스템 이해
        ↓
관심 기능의 상세 문서
        ↓
실제 Runtime / Config
        ↓
Validation / Problem Solving
```

파일명을 추측하지 않아도 문서를 따라갈 수 있도록 README와 각 문서에서 다음 페이지를 연결합니다.

---

## 22. 최종 구조 요약

```text
ai_perception/
│
├── README.md
│   └── 프로젝트 전체 문서 진입점
│
├── config/
│   └── Runtime / Contract / Camera / Threshold 설정
│
├── scripts/
│   ├── Incoming Inspection
│   ├── PRE_ROOF
│   ├── Integration
│   └── Unity Video
│
└── docs/
    ├── 01~09 상세 문서
    ├── 실제 검증 이미지
    └── 실제 검증 영상 링크
```

최종 GitHub에는 개발 폴더 전체를 그대로 복사하지 않고, 실제 구현·검증·기술적 의사결정이 추적되도록 필요한 코드와 문서를 정리합니다.

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
10. **현재 문서 — 프로젝트 구조**
