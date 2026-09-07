# 06. 프로젝트 범위
이 문서는 팀 전체 시스템과 **직접 담당한 AI Perception / Vision 영역**의 경계를 명확히 구분합니다.

세부 검증 상태는 [05. 검증](05_validation.md), 저장소 구성은 [07. 프로젝트 구조](07_project_structure.md)에서 별도로 정리합니다.

---

## 1. 전체 팀 시스템

팀 프로젝트 전체는 다음 영역으로 구성됩니다.

```text
AI 기반 조립식 주택 자동화 공장
├─ TurtleBot / Logistics
├─ FR5 / Robot Operation
├─ Team Server / FMS
├─ Unity / Digital Twin
└─ AI Perception / Vision
```

이 문서에서 직접 구현 범위로 설명하는 대상은 **AI Perception / Vision**입니다.

---

## 2. 직접 담당 범위

### 2.1 Global Vision

직접 설계·구현·검증한 주요 범위:

- Global Camera RTSP 입력 구조
- FFmpeg Low-Latency Direct Pipe
- ROS2 Global Camera Node
- `/vision/global_camera/image_raw`
- HOUSE B Auto Alignment
- `/vision/global_camera/image_aligned`
- BASE A/B Incoming QA
- HOUSE B Incoming QA
- HOUSE A Incoming QA
- Incoming Inspection Runtime
- Incoming UI
- `/vision/incoming_qa/annotated_image`
- Unified Inspection Runtime 연동
- Inspection Recipe / Model Contract 연동
- Team Server UDP Request / ACK / Result Interface
- Duplicate / Conflict / Correlation 처리
- SQLite Transaction Store
- Unity HMV1 `stream_id=1` Video Sender
- Global Vision Runtime 및 실제 통신 연동 검증

---

### 2.2 Depth Vision

직접 설계·구현·검증한 주요 범위:

- D435 RGB / Depth Image 입력 구조
- PRE_ROOF TOP Runtime
- PRE_ROOF LEFT Runtime
- PRE_ROOF RIGHT Runtime
- PRE_ROOF FRONT Runtime
- PRE_ROOF BEHIND Runtime
- View별 Golden / ROI / Threshold / Metric Profile
- PRE_ROOF 5-View Dashboard
- Integration Controller V3
- HOLD / INSPECT 상태 관리
- `expected_view`
- Runtime Result Normalize
- View Commit
- Overall 계산
- Final Result Ready
- Reinspection Cycle
- Persistent State 보호
- PRE_ROOF Server Wire Contract
- UDP Gateway V2 / V3
- Duplicate / Conflict / Idempotency 처리
- Unity HMV1 `stream_id=2` Video Sender
- Controller Self-Test
- Dummy Server Full E2E
- Local HMV1 Wire E2E

---

## 3. Team Server / FMS와의 책임 경계

### Vision 측 구현

- UDP Inspection Request 수신
- Request Schema Validation
- ACK 생성
- Duplicate 처리
- Conflict 차단
- Vision-side Transaction Persistence
- Result Correlation
- Final Result 송신
- Reinspection Cycle 연동

### Team Server / FMS 영역

다음 영역은 Team Server/FMS 담당입니다.

- 공정 전체 Business Logic
- Job / Order 관리
- 전체 생산 상태 관리
- FMS 내부 DB 구조
- 공정 Source of Truth
- 다른 설비와의 Scheduling

Vision은 Server/FMS의 Request를 받아 검사하고 Result를 반환하는 **AI Perception Interface**를 담당합니다.

---

## 4. Unity와의 책임 경계

### Vision 측 구현

- Annotated ROS2 Image 생성
- JPEG Encoding
- HMV1 Header 구성
- Frame Chunk 분할
- UDP 송신
- Global `stream_id=1`
- PRE_ROOF `stream_id=2`
- Wire Size 검증
- Video E2E 검증

### Unity 영역

다음 영역은 Unity 담당입니다.

- Digital Twin Scene 구성
- 3D Object 상태 표현
- Unity UI 내부 구현
- Server 기반 공정 상태 Rendering
- Unity 내부 State Machine
- Robot Animation

Vision → Unity 직접 연결은 **Annotated Video 전송 전용**입니다.

---

## 5. Robot과의 책임 경계

### Vision 역할

- View별 검사 조건 정의
- 해당 View Frame 검사
- View Result 생성
- Controller에 Result 전달

### Robot 역할

- View Pose 이동
- Manipulation
- Pick / Place
- 실제 조립 동작
- Motion Planning
- Joint / Cartesian Motion 실행

Vision은 Robot Motion Command를 직접 생성하지 않습니다.

현재 실제 장비 통합 운용 기준에서는 View Pose를 수동으로 전환합니다.

---

## 6. Camera 장비와의 책임 경계

### Global Camera

직접 구현한 범위:

```text
RTSP Input
→ FFmpeg
→ BGR Frame
→ ROS2 Image
```

스마트폰 Camera와 RTSP Camera App 자체는 외부 입력 장비입니다.

### Intel RealSense D435

직접 구현한 범위:

- Vision 측 RGB / Depth Image 입력 사용
- PRE_ROOF Runtime 입력 처리
- View별 품질 검사

D435 Driver 자체 구현과 Robot Control PC의 Camera Server 내부 구현은 담당 범위가 아닙니다.

---

## 7. AI Model / Dataset 범위

직접 수행한 주요 영역:

- Synthetic Dataset 구성
- Class 구조 설계
- Global / Local Camera 조건 분리
- Real Camera Validation
- 취약 구간 데이터 보강
- Fine-tuning 계보 관리
- Runtime Model Contract
- Inspection Recipe
- ROI / Threshold 기반 운영 보정
- Attempt 계보별 검증

공개 저장소에는 핵심 Runtime 코드와 Contract / Recipe, 데이터 구성 및 검증 결과 요약만 포함합니다.

대용량 Raw / Synthetic Dataset과 Model Weight 전체는 공개 코드에서 분리합니다.

---

## 8. 설계상 제외한 기능

다음 기능은 AI Perception 직접 담당 범위에 포함하지 않습니다.

- Vision 기반 Robot 직접 제어
- 자동 Robot View Transition Command
- PLC Control
- TurtleBot Navigation
- FR5 Motion Planning
- Unity Digital Twin 내부 구현
- Team Server/FMS 내부 Business Logic

PRE_ROOF의 현재 역할 분리는 다음과 같습니다.

```text
Server Request
→ Vision 상태 관리
→ Robot View 이동
→ Vision 검사
→ View Result
→ Overall
→ Final Result
```

---

## 9. 검증 책임 범위

### Global Vision

직접 검증한 범위에는 다음이 포함됩니다.

- Global Camera 실제 입력
- ROS2 Image Runtime
- Incoming Inspection Runtime
- Annotated Image
- Team Server Communication E2E
- Unity HMV1 E2E

전체 실제 물리검사 Production Scenario의 최종 인수 상태는 별도로 관리합니다.

### Depth Vision / PRE_ROOF

직접 검증한 범위에는 다음이 포함됩니다.

- D435 RGB / Depth Image 입력
- 5-View Runtime
- Controller V3 Self-Test
- Dummy Server Full E2E
- Local HMV1 Wire E2E

Actual Team Server / Unity / D435 + Robot 통합 상태는 [05. 검증](05_validation.md)에서 구분해 기록합니다.

---

## 10. 공개 범위 원칙

`ai_perception`은 개발 Workspace 전체 복사본이 아니라 최종 구현 이해에 필요한 코드만 선별한 영역입니다.

포함:

- 핵심 Runtime
- Interface 코드
- Contract / Recipe
- 기술 문서
- 대표 검증 자료

제외:

- Raw Dataset
- Synthetic Dataset 전체
- Model Weight
- Runtime DB
- 대량 Capture / Audit
- Probe / Canary
- Legacy / Backup / Cache
- 인증정보
- 개인 환경 파일

이 원칙은 팀 시스템 전체를 개인 구현으로 보이게 하지 않고, 실제 직접 담당한 Vision 영역을 명확하게 보여주기 위한 것입니다.

---

## 11. 표현 원칙

문서에서는 다음 구분을 유지합니다.

```text
직접 구현
→ Vision Runtime / Controller / Gateway / Interface

연동
→ Team Server / Unity / Robot

검증
→ 실제 확인한 범위만 PASS
```

팀원이 구현한 Server, Unity, Robot 내부 기능은 개인 구현으로 표현하지 않습니다.

---

## 상세 문서

1. [프로젝트 개요](01_overview.md)
2. [시스템 아키텍처](02_architecture.md)
3. [주요 기능](03_features.md)
4. [데이터 흐름](04_data_flow.md)
5. [검증](05_validation.md)
6. [프로젝트 범위](06_project_scope.md)
7. [프로젝트 구조](07_project_structure.md)
