# 02. 시스템 아키텍처

## 전체 구조

AI Perception 시스템은 **Global Vision**과 **Depth Vision**을 독립 Runtime으로 구성하고, 결과 단계에서 Team Server/FMS 및 Unity와 연결합니다.

```mermaid
flowchart LR
    subgraph GV[Global Vision]
        CAM[RTSP Camera]
        FF[FFmpeg Direct Pipe]
        RAW[/vision/global_camera/image_raw]
        ALIGN[HOUSE B Auto Alignment]
        ALIGNED[/vision/global_camera/image_aligned]
        IQA[Incoming QA Runtime]

        CAM --> FF --> RAW
        RAW --> IQA
        RAW --> ALIGN --> ALIGNED --> IQA
    end

    subgraph DV[Depth Vision]
        D435[RealSense D435]
        RPC[Robot Control PC]
        VIEWS[PRE_ROOF 5-View Runtime]
        CTRL[Integration Controller V3]

        D435 --> RPC --> VIEWS --> CTRL
    end

    IQA --> SG[Incoming UDP Gateway]
    CTRL --> PG[PRE_ROOF UDP Gateway]

    SG --> SERVER[Team Server / FMS]
    PG --> SERVER

    IQA --> H1[HMV1 stream_id=1]
    CTRL --> H2[HMV1 stream_id=2]

    H1 --> UNITY[Unity]
    H2 --> UNITY
```

---

## 1. Global Vision 아키텍처

### 1.1 Global Camera 입력

Global Camera는 스마트폰 RTSP 영상을 Ubuntu Vision PC에서 FFmpeg로 직접 디코딩한 뒤 ROS2 Image Topic으로 Publish합니다.

```text
Smartphone Camera
    ↓ RTSP/RTP
FFmpeg Low-Latency Direct Pipe
    ↓ BGR Frame
ROS2 Global Camera Node
    ↓
/vision/global_camera/image_raw
```

Runtime 기준:

| 항목 | 값 |
|:---|:---|
| Resolution | `1280x720` |
| Encoding | `BGR8` |
| QoS | `BEST_EFFORT` |
| History | `KEEP_LAST 1` |

Camera Password는 코드에 직접 저장하지 않고 `OLD_BIRD_PASSWORD` 환경변수로 주입합니다.

Global Camera Node는 일시적인 FFmpeg decode 오류 이후 Frame Stream이 복구될 수 있도록 Runtime 상태를 관리합니다.

---

### 1.2 HOUSE B Auto Alignment

HOUSE B는 Raw Camera Frame을 직접 검사하지 않고, 고정 작업대 Reference와 현재 Frame을 비교해 정렬된 Frame을 생성합니다.

```text
/vision/global_camera/image_raw
    ↓
HOUSE B Auto Alignment
    ↓ Gate PASS
/vision/global_camera/image_aligned
```

정렬 기준은 부품 자체가 아니라 작업대의 고정 구조입니다.

주요 Anchor:

- Tape
- Board
- Background
- Corner Marker
- Material ROI 외곽 구조

Material ROI 내부는 Alignment 계산에서 제외합니다.

Gate 기준:

| 항목 | 기준 |
|:---|:---:|
| ECC | `>= 0.65` |
| Shift | `<= 60 px` |
| Rotation | `<= 3°` |

Gate를 통과하지 못하면 Aligned Topic을 Publish하지 않습니다.

이를 통해 작업대 위치가 크게 어긋난 상태에서 HOUSE B 검사가 진행되는 것을 차단합니다.

---

### 1.3 Incoming QA Runtime

Incoming QA는 검사 Mode에 따라 Camera Source를 분리합니다.

| 검사 Mode | Camera Source |
|:---|:---|
| BASE A/B | `/vision/global_camera/image_raw` |
| HOUSE B | `/vision/global_camera/image_aligned` |
| HOUSE A | `/vision/global_camera/image_raw` |

검사 UI의 최종 기준 Canvas는 `1280x840`입니다.

Unity에 전달하는 Annotated Image는 UI 전체가 아니라 Camera 영역만 분리해 Publish합니다.

```text
UI Canvas
1280x840
    ↓ camera area crop
1280x720 BGR8
    ↓
/vision/incoming_qa/annotated_image
```

이 구조를 사용해 기존 Inspection Geometry를 변경하지 않고 Unity Video Interface를 추가했습니다.

---

### 1.4 Incoming Runtime Layer

Incoming Runtime은 기능을 계층적으로 확장한 Final Chain을 사용합니다.

```text
Final V1
  ↓
Final V2
  ↓
Final V3
  ↓
Final V4 UI
```

역할:

- **V1**: 기본 Incoming Inspection Runtime
- **V2**: Server Transaction Integration
- **V3**: Unity용 Annotated Image Topic 추가
- **V4 UI**: 검사 Logic은 유지하고 Palette / Text Visibility만 조정

V4는 ROI, Threshold, Pixel Position, Server Integration, Unity Integration을 변경하지 않는 UI Layer입니다.

---

## 2. Global Server/FMS Interface

### 2.1 Request / ACK / Result

Global Incoming의 통신 구조는 다음과 같습니다.

```text
Team Server
    ↓ UDP Request :20051
Vision UDP Gateway
    ↓ ROS2 Request Topic
Incoming QA Runtime
    ↓
Inspection Result
    ↓ ROS2 Result Topic
Vision UDP Gateway
    ↓ UDP Result :20052
Team Server
```

ACK는 Request Datagram의 Source IP / Source Port로 반환합니다.

ACK 의미:

```text
Request received
+
Transaction accepted
```

ACK는 검사 완료를 의미하지 않습니다.

---

### 2.2 Transaction Store

Vision 측 Gateway는 SQLite를 이용해 Transaction 상태를 관리합니다.

주요 목적:

- Request 기록
- Duplicate Request 처리
- Conflict Request 차단
- Result Correlation
- Final Result 상태 보존

검사 Transaction의 공정 전체 Source of Truth는 Team Server/FMS가 담당합니다.

Vision DB는 Vision 측 통신 및 검사 Transaction 보호를 위한 Runtime Persistence 역할을 합니다.

---

## 3. Global Unity Interface

Global Incoming은 다음 Annotated Topic을 HMV1 UDP Video로 변환합니다.

```text
/vision/incoming_qa/annotated_image
    ↓
JPEG Encoding
    ↓
HMV1
    ↓ stream_id=1
UDP :21010
    ↓
Unity
```

HMV1 기준:

| 항목 | 값 |
|:---|:---|
| Version | `1` |
| Header | `32 bytes` |
| JPEG Quality | `80` |
| Target FPS | `10` |
| Payload Max | `1200 bytes` |
| Datagram Max | `1232 bytes` |
| Stream ID | `1` |

Vision → Unity 직접 연결은 Annotated Video 전용입니다.

공정 상태는 Team Server/FMS를 Source of Truth로 사용합니다.

---

## 4. Depth Vision 아키텍처

### 4.1 D435 입력 구조

Intel RealSense D435는 Robot Control PC에 연결되어 있습니다.

Vision PC는 D435를 직접 USB로 소유하지 않고 Robot Control PC의 Image Endpoint를 통해 입력을 받습니다.

```text
Intel RealSense D435
    ↓
Robot Control PC
    ├─ RGB Image
    └─ Depth Image
         ↓
Vision Runtime
```

현재 Runtime 입력:

| 항목 | 형식 |
|:---|:---|
| RGB | JPEG `1280x720` |
| Depth Image | JPEG `1280x720` |

현재 `/depthimg`는 시각화용 JPEG Depth Image이며 metric 16-bit Raw Depth와 동일한 데이터로 취급하지 않습니다.

---

## 5. PRE_ROOF 5-View Runtime

PRE_ROOF 검사는 하나의 공통 조립 품질 검사이지만 Camera 방향별 가시성 차이 때문에 View를 분리합니다.

```text
TOP
→ LEFT
→ RIGHT
→ FRONT
→ BEHIND
```

각 View Runtime은 독립적으로 다음 Profile을 가집니다.

- Golden
- ROI
- Threshold
- Metric

최종 Runtime 기준:

| View | Runtime |
|:---|:---|
| TOP | V5 |
| LEFT | V3 |
| RIGHT | V7 |
| FRONT | V1 |
| BEHIND | V4 |

View별 Runtime은 실제 Image Inspection에 집중하고 전체 검사 상태는 Integration Controller가 관리합니다.

---

## 6. PRE_ROOF Integration Controller

Integration Controller V3는 하나의 Server Request를 하나의 5-View Inspection Cycle로 관리합니다.

```text
Server Request
    ↓
HOLD / expected_view=TOP
    ↓
TOP Inspection
    ↓
LEFT
    ↓
RIGHT
    ↓
FRONT
    ↓
BEHIND
    ↓
Overall
    ↓
Final Result Ready
```

Controller 주요 역할:

- Server Request Correlation
- Inspection Cycle 관리
- `expected_view` 관리
- View Result Normalize
- View Commit
- Overall 계산
- Final Result Ready
- Reinspection Cycle
- Persistent State 보호

Runtime 결과 정규화:

```text
PASS           → PASS
FAIL           → FAIL
NOT_EVALUATED  → NOT_EVALUATED
ERROR          → NOT_EVALUATED
RUNTIME_OFFLINE → View Commit 차단
RESULT_NOT_READY → View Commit 차단
```

Runtime Offline 또는 Result Not Ready 상태에서는 `expected_view`를 유지하고 Final Result를 생성하지 않습니다.

---

## 7. PRE_ROOF Result 규칙

공식 View Result:

```text
PASS
FAIL
NOT_EVALUATED
```

Overall 규칙:

```text
5/5 PASS
→ PASS

5개 View 완료 + FAIL 1개 이상
→ FAIL

NOT_EVALUATED 1개 이상
→ NOT_EVALUATED

5개 View 미완료
→ Final Result 생성 안 함
```

이 규칙은 View Runtime의 세부 Metric과 Integration State Machine을 분리하기 위한 공통 Contract입니다.

---

## 8. PRE_ROOF Server/FMS Interface

통신 구조:

```text
Team Server
    ↓ UDP Request :20061
PRE_ROOF UDP Gateway V3
    ↓ HTTP
Integration Controller V3
    ↓
5-View Inspection
    ↓
Final Result
    ↓
PRE_ROOF UDP Gateway V3
    ↓ UDP Result :20062
Team Server
```

Gateway 주요 기능:

- Request Validation
- ACK
- Duplicate 처리
- `REQUEST_CONFLICT`
- Controller-before-ACK
- SQLite Persistence
- Final Result Watcher
- Result Idempotency
- `RESULT_CONFLICT`

ACK는 검사 완료가 아니라 Request 수신 및 Transaction 수락을 의미합니다.

---

## 9. PRE_ROOF Unity Interface

PRE_ROOF는 Global Incoming에서 검증한 HMV1 Wire Format을 재사용합니다.

```text
/vision/pre_roof/annotated_image
    ↓
JPEG Encoding
    ↓
HMV1
    ↓ stream_id=2
UDP :21020
    ↓
Unity
```

기준:

| 항목 | 값 |
|:---|:---|
| HMV1 Version | `1` |
| Stream ID | `2` |
| Resolution | `1280x720` |
| JPEG Quality | `80` |
| Target FPS | `10` |
| Payload Max | `1200 bytes` |
| Datagram Max | `1232 bytes` |

Global과 PRE_ROOF는 같은 Wire Format을 사용하지만 Stream ID와 Port를 분리합니다.

---

## 10. Robot과 Vision의 책임 경계

PRE_ROOF 실제 운용에서 Robot은 View Pose 이동을 담당합니다.

Vision은 Robot Motion Command를 직접 생성하지 않습니다.

```text
Robot
→ View Pose 이동

Vision
→ 해당 View 검사
→ View Result 생성
```

현재 Actual E2E 기준에서는 TOP / LEFT / RIGHT / FRONT / BEHIND Pose 이동을 수동 운영합니다.

이를 통해 Vision Inspection Logic과 Robot Control Logic을 분리했습니다.

---

## 11. 시스템 책임 분리

### Vision

- Camera Input
- Image Processing
- Alignment
- Inspection
- Result Generation
- Annotated Video
- Vision-side Transaction Protection

### Team Server / FMS

- Inspection Request
- 공정 Transaction 관리
- 공정 상태 관리
- Result 저장
- Source of Truth

### Unity

- Digital Twin
- Vision Annotated Video 표시
- Server 기반 공정 상태 시각화

### Robot

- View Pose 이동
- Manipulation
- 실제 공정 동작

이 구조를 통해 Vision이 공정 전체 Control Authority를 가지지 않고, 각 시스템의 책임을 분리했습니다.
