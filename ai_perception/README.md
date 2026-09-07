# AI Perception / Vision

조립식 주택 자동화 공정에서 **Global Vision**과 **Depth Vision**을 이용해 자재 입고 상태와 조립 품질을 검사하고, Vision 결과를 Team Server/FMS 및 Unity와 연동하는 AI Perception 시스템입니다.

---

## 프로젝트 정보

| 항목 | 내용 |
|:---|:---|
| 프로젝트 | AI 기반 조립식 주택 자동화 공장 |
| 형태 | 팀 프로젝트 |
| 담당 영역 | AI Perception / Vision |
| Vision 구성 | Global Vision / Depth Vision |
| 개발 환경 | Ubuntu 24.04, ROS2 Jazzy, Python |
| 주요 기술 | OpenCV, PyTorch, FFmpeg, ROS2, UDP, HTTP, SQLite, Pydantic |
| 카메라 | Global RTSP Camera / Intel RealSense D435 |

---

## 프로젝트 개요

전체 팀 시스템은 TurtleBot, FR5 협동로봇, Team Server/FMS, Unity Digital Twin, AI Perception으로 구성됩니다.

이 디렉터리의 `ai_perception` 영역은 그중 **Vision 담당 구현만** 정리합니다.

```text
AI 기반 조립식 주택 자동화 공장
├─ TurtleBot / Logistics
├─ FR5 / Robot Operation
├─ Team Server / FMS
├─ Unity / Digital Twin
└─ AI Perception / Vision
   ├─ Global Vision
   │  ├─ Global Camera
   │  ├─ Incoming QA
   │  ├─ HOUSE B Auto Alignment
   │  ├─ Inspection Runtime
   │  ├─ Server Interface
   │  └─ Unity Video Interface
   └─ Depth Vision
      ├─ RealSense D435
      ├─ PRE_ROOF 5-View Inspection
      ├─ Integration Controller
      ├─ Server Interface
      └─ Unity Video Interface
```

---

## 개인 담당

### Global Vision

- Global Camera RTSP 수신 구조 설계 및 구현
- FFmpeg Low-Latency Direct Pipe 기반 영상 수신
- ROS2 Global Camera Image Topic 구성
- Global Incoming QA Runtime 구현
- BASE A/B, HOUSE A, HOUSE B 검사 Mode 구성
- HOUSE B Auto Alignment 구현
- Incoming QA UI 및 Annotated Image Topic 구현
- Vision ↔ Team Server UDP Interface 구현
- Request / ACK / Final Result Transaction 처리
- SQLite 기반 Vision Transaction 관리
- Vision → Unity HMV1 Video Interface 구현
- Global Vision Runtime 및 E2E 검증

### Depth Vision

- Intel RealSense D435 RGB / Depth 입력 구조 구성
- PRE_ROOF TOP / LEFT / RIGHT / FRONT / BEHIND 5-View 검사 구현
- View별 Golden / ROI / Threshold / Metric Profile 구성
- PRE_ROOF 5-View Dashboard 구현
- Integration Controller V3 구현
- HOLD / Inspection / View Commit / Overall / Final Result 상태 관리
- PRE_ROOF Server Wire Contract 구현
- PRE_ROOF UDP Gateway 구현
- Vision → Unity HMV1 `stream_id=2` Interface 구현
- Controller Self-Test 및 Dummy Full E2E 검증
- Local HMV1 Wire E2E 검증

Robot Motion Planning, FR5 제어, TurtleBot Navigation, Team Server/FMS 내부 로직, Unity Digital Twin 내부 구현은 다른 담당 영역입니다.

이 프로젝트에서는 해당 시스템들과 연결되는 **Vision 측 Interface와 데이터 송수신 및 검증 범위**를 담당했습니다.

---

## 핵심 구현

### Global Vision

```text
RTSP Camera
→ FFmpeg Low-Latency Direct Pipe
→ ROS2 Image
→ Incoming QA / Auto Alignment
→ Inspection Result
→ Team Server / Unity
```

주요 구현:

- `/vision/global_camera/image_raw` `1280x720 BGR8`
- HOUSE B ECC 기반 Auto Alignment
- BASE A/B / HOUSE B / HOUSE A Incoming QA
- `/vision/incoming_qa/annotated_image`
- UDP Request / ACK / Final Result
- SQLite Transaction 관리
- HMV1 `stream_id=1` Unity Video

### Depth Vision

```text
RealSense D435
→ RGB / Depth Image
→ TOP
→ LEFT
→ RIGHT
→ FRONT
→ BEHIND
→ Overall
→ Final Result
```

각 View는 독립적인 Golden / ROI / Threshold / Metric Profile을 사용하고, Controller V3가 하나의 Inspection Cycle로 통합합니다.

Runtime Offline 또는 Result Not Ready 상태에서는 View Commit과 Final Result 생성을 차단합니다.

---

## 시스템 연동

| 영역 | Interface |
|:---|:---|
| Global Camera | RTSP → FFmpeg → ROS2 |
| Global Server | UDP `20051 / 20052` |
| Global Unity | HMV1 `stream_id=1`, UDP `21010` |
| D435 | RGB / Depth Image over HTTP |
| PRE_ROOF Server | UDP `20061 / 20062` |
| PRE_ROOF Unity | HMV1 `stream_id=2`, UDP `21020` |

ACK는 검사 완료가 아니라 **Request 수신 및 Transaction 수락**을 의미합니다.

공정 전체 상태의 Source of Truth는 Team Server/FMS가 담당합니다.

---

## 검증 결과

### Global Vision

| 검증 항목 | 결과 |
|:---|:---:|
| Global Camera ROS2 Frame | PASS |
| Incoming Annotated Image | PASS |
| Team Server Request / ACK / Result E2E | PASS |
| Unity HMV1 실제 영상 E2E | PASS |
| 전체 실제 물리검사 Production Scenario | 미완료 |

Team Server 통신 E2E는 Request / ACK / Result Contract 검증이며 실제 물리검사 Production PASS와 구분합니다.

### Depth Vision / PRE_ROOF

| 검증 항목 | 결과 |
|:---|:---:|
| D435 RGB `1280x720` 입력 | PASS |
| D435 Depth Image `1280x720` 입력 | PASS |
| PRE_ROOF 5-View Runtime | PASS |
| Controller V3 Self-Test | PASS |
| Dummy Server 5-View Full E2E | PASS |
| HMV1 `stream_id=2` Local E2E | PASS |
| Actual Team Server PRE_ROOF | 대기 |
| Actual Unity PRE_ROOF | 대기 |
| D435 + Manual Robot 5-View Actual E2E | 대기 |

Dummy / Local 검증과 실제 시스템 E2E를 구분하며, 완료되지 않은 항목은 Production PASS로 표현하지 않습니다.

---

## 기술 스택

- **OS / Middleware**: Ubuntu 24.04, ROS2 Jazzy
- **Language**: Python
- **Vision / AI**: OpenCV, NumPy, PyTorch
- **Camera / Video**: RTSP, FFmpeg, Intel RealSense D435
- **Communication**: ROS2 Topic, UDP, HTTP
- **Data / Schema**: JSON, Pydantic, SQLite
- **Visualization**: HMV1 JPEG over UDP

---

## 저장소 구조

```text
ai_perception/
├─ README.md
├─ config/
│  └─ global_vision/
├─ scripts/
│  ├─ global_vision/
│  └─ depth_vision/
└─ docs/
   ├─ README.md
   ├─ 01_overview.md
   ├─ 02_architecture.md
   ├─ 03_features.md
   ├─ 04_data_flow.md
   ├─ 05_validation.md
   ├─ 06_project_scope.md
   ├─ 07_project_structure.md
   └─ images/
```

Raw / Synthetic Dataset 전체, Model Weight, Runtime DB, Probe, Canary, Legacy, Backup 파일은 공개 저장소에서 제외합니다.

---

## 상세 문서

1. [프로젝트 개요](docs/01_overview.md)
2. [시스템 아키텍처](docs/02_architecture.md)
3. [주요 기능](docs/03_features.md)
4. [데이터 흐름](docs/04_data_flow.md)
5. [검증](docs/05_validation.md)
6. [프로젝트 범위](docs/06_project_scope.md)
7. [프로젝트 구조](docs/07_project_structure.md)

이미지와 실제 동작 영상은 최종 통합 검증 완료 후 2차 작업에서 추가합니다.
