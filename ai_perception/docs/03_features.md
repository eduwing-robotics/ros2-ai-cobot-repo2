
# 03. 주요 기능
AI Perception 시스템에서 직접 구현한 주요 기능을 **Global Vision**과 **Depth Vision**으로 구분해 정리합니다.

세부 시스템 구조는 [02. 시스템 아키텍처](02_architecture.md), 실제 데이터 흐름은 [04. 데이터 흐름](04_data_flow.md), 검증 상태는 [05. 검증](05_validation.md)에서 별도로 확인할 수 있습니다.

---

## 1. Global Vision

### 1.1 Global Camera ROS2 Pipeline

스마트폰 RTSP 영상을 Ubuntu Vision PC에서 수신해 ROS2 Image Topic으로 변환합니다.

주요 기능:

- RTSP/RTP UDP 영상 입력
- FFmpeg Low-Latency Direct Pipe
- BGR Frame 처리
- ROS2 `sensor_msgs/Image` Publish
- `1280x720`, `BGR8`
- `BEST_EFFORT`, `KEEP_LAST 1`
- Camera 인증정보 환경변수 분리
- 일시적 Decode 오류 이후 Stream Recovery 상태 관리

주요 Topic:

```text
/vision/global_camera/image_raw
```

---

### 1.2 HOUSE B Auto Alignment

HOUSE B 검사 전 작업대 위치를 Reference 기준으로 자동 정렬합니다.

주요 기능:

- ECC 기반 Alignment
- Shift / Rotation 계산
- 고정 작업대 구조 기반 Anchor
- Material ROI 내부 Alignment 계산 제외
- Gate Reject 시 Aligned Topic 생성 차단

Gate 기준:

| 항목 | 기준 |
|:---|:---:|
| ECC | `>= 0.65` |
| Shift | `<= 60 px` |
| Rotation | `<= 3°` |

출력 Topic:

```text
/vision/global_camera/image_aligned
```

---

### 1.3 Incoming QA 3-Mode Inspection

Incoming QA는 조립 상태에 따라 세 검사 Mode를 지원합니다.

| Mode | 검사 대상 | Camera Source |
| --- | --- | --- |
| BASE A/B | Base A / Base B | Raw |
| HOUSE B | B01~B06 | Raw |
| HOUSE A | A01~A07 | Raw |

Mode별로 필요한 Camera Source와 ROI / 검사 Logic을 분리했습니다.

#### HOUSE B - 베이스 B자재 정상품 판정

HOUSE B 수입검사에서 정상 자재를 검사하고 PASS로 판정하는 과정입니다.

https://github.com/user-attachments/assets/8568c3e3-7e25-4f02-8cb6-c659a05fa915

#### HOUSE B - 베이스 B자재 불량 판정

HOUSE B 수입검사에서 불량 자재를 검출하고 FAIL로 판정하는 과정입니다.

https://github.com/user-attachments/assets/5fd878ed-f8ff-4fef-872c-ecb6a15aa08e

---

### 1.4 Incoming Final Runtime Chain

검증된 기능을 유지하면서 기능을 단계적으로 확장하도록 Final Runtime Chain을 구성했습니다.

```text
Final V1
→ Final V2
→ Final V3
→ Final V4 UI
```

| Runtime | 역할 |
|:---|:---|
| V1 | 기본 Incoming Inspection |
| V2 | Server Transaction Integration |
| V3 | Unity Annotated Image Topic |
| V4 UI | Palette / Text Visibility 개선 |

UI 변경이 검사 ROI, Threshold, Pixel 좌표, Server / Unity Contract를 변경하지 않도록 분리했습니다.

---

### 1.5 Incoming QA UI

최종 UI는 검사 상태와 Slot 결과를 표시합니다.

주요 상태:

- HOLD
- INSPECT
- RESULT
- WAITING

Canvas 기준:

```text
1280x840
```

Camera 영역:

```text
1280x720
```

---

### 1.6 Annotated Image Publish

Incoming UI의 Camera 영역만 분리해 Unity용 ROS2 Image Topic으로 Publish합니다.

주요 Topic:

```text
/vision/incoming_qa/annotated_image
```

Frame 기준:

```text
1280x720
BGR8
step=3840
```

---

### 1.7 Unified Inspection Runtime

Incoming QA의 AI 검사와 운영 Contract를 연결하는 Runtime입니다.

주요 기능:

- Runtime Model 로드
- Inspection Recipe 적용
- Model Contract 검증
- Slot별 ROI 처리
- Classification / Quality 판단
- Runtime 상태 안정화
- Mode별 Result 생성

현재 공개 저장소에는 Runtime 코드와 Contract / Recipe를 포함하고, Model Weight와 대용량 Dataset은 제외합니다.

---

### 1.8 Team Server/FMS Interface

Global Incoming Vision은 Team Server/FMS와 UDP Transaction을 처리합니다.

주요 기능:

- Inspection Request 수신
- Request Schema Validation
- ACK
- Duplicate 처리
- Payload Conflict 차단
- Final Result 송신
- Request / Result Correlation

통신 성공과 검사 완료 의미를 분리해 운영합니다.

```text
ACK
= Request 수신 / Transaction 수락

Final Result
= 검사 완료 후 생성되는 결과
```

---

### 1.9 SQLite Transaction Store

Vision 측 Transaction 보호를 위해 SQLite Store를 사용합니다.

주요 기능:

- Request 상태 기록
- ACK 상태 기록
- Duplicate 보호
- Final Result 상태 저장
- Result Correlation
- Runtime 재시작 시 Transaction 보호

공정 전체 Source of Truth는 Team Server/FMS가 담당합니다.

---

### 1.10 Unity HMV1 Video Sender

Incoming Annotated Image를 JPEG로 인코딩하고 HMV1 UDP Packet으로 Unity에 전달합니다.

주요 기준:

| 항목 | 값 |
|:---|:---|
| Stream ID | `1` |
| Resolution | `1280x720` |
| JPEG Quality | `80` |
| Target FPS | `10` |
| Header | `32 bytes` |
| Payload Max | `1200 bytes` |
| Datagram Max | `1232 bytes` |

Vision → Unity 직접 연결은 Annotated Video 전송 전용입니다.

---

## 2. Depth Vision

### 2.1 D435 RGB / Depth 입력

Intel RealSense D435는 Robot Control PC에 연결되어 있으며 Vision PC는 네트워크 Endpoint를 통해 영상을 사용합니다.

현재 입력:

| 입력 | 형식 |
|:---|:---|
| RGB | JPEG `1280x720` |
| Depth Image | JPEG `1280x720` |

현재 Depth Image는 시각화용 JPEG이며 metric 16-bit Raw Depth와 동일하게 취급하지 않습니다.

---

### 2.2 PRE_ROOF 5-View Inspection

조립 구조물을 다섯 방향으로 분리해 검사합니다.

```text
TOP
LEFT
RIGHT
FRONT
BEHIND
```

각 View는 독립적인 다음 자산을 사용합니다.

- Golden
- ROI
- Threshold
- Metric Profile

---

### 2.3 TOP Runtime

최종 Runtime:

```text
harmony_pre_roof_qc_top_runtime_v5.py
```

주요 특징:

- `VIEW_TOP_FIXED`
- `IDENTITY`
- Golden V2
- ROI V3
- Threshold V2
- Base Hole Profile

---

### 2.4 LEFT Runtime

최종 Runtime:

```text
harmony_pre_roof_qc_left_runtime_v3.py
```

주요 특징:

- `VIEW_LEFT_FIXED`
- `ROTATE_180`
- Golden V1
- ROI V2
- Threshold V1
- Lift Threshold V1

---

### 2.5 RIGHT Runtime

최종 Runtime:

```text
harmony_pre_roof_qc_right_runtime_v7.py
```

주요 특징:

- `VIEW_RIGHT_FIXED`
- `ROTATE_180`
- Golden V2
- ROI V1
- Wall / Lift Threshold 분리

---

### 2.6 FRONT Runtime

최종 Runtime:

```text
harmony_pre_roof_qc_front_runtime_v1.py
```

주요 특징:

- `VIEW_FRONT_FIXED`
- `ROTATE_180`
- Golden V1
- ROI V2
- Threshold V1

---

### 2.7 BEHIND Runtime

최종 Runtime:

```text
harmony_pre_roof_qc_behind_runtime_v4.py
```

주요 특징:

- `VIEW_BEHIND_FIXED`
- `IDENTITY`
- Golden V1
- ROI V3
- CENTER_STRUCTURE Profile
- Structure Threshold V1

---

### 2.8 5-View Dashboard

Dashboard V17은 5개 View의 검사 상태를 한 화면에서 표시합니다.

표시 항목:

- Current View
- Expected View
- View별 Result
- Inspection 상태
- Overall
- Final Result 준비 상태

Dashboard는 Visual Layer이고 상태 전이 Authority는 Integration Controller V3가 담당합니다.

---

### 2.9 Integration Controller V3

하나의 Server Request를 하나의 PRE_ROOF 5-View Inspection Cycle로 관리합니다.

주요 기능:

- Request Correlation
- HOLD / INSPECT
- `expected_view`
- TOP → LEFT → RIGHT → FRONT → BEHIND 순서 관리
- Runtime Result Normalize
- View Commit
- Overall 계산
- Final Result Ready
- Reinspection Cycle
- Persistent State 보호

---

### 2.10 Runtime Result Normalize

각 View Runtime의 결과를 공통 결과로 정규화합니다.

```text
PASS
FAIL
NOT_EVALUATED
```

Runtime Offline 또는 Result Not Ready 상태에서는 View Commit을 차단하고 현재 `expected_view`를 유지합니다.

---

### 2.11 Overall Result

5개 View 완료 후 Overall을 계산합니다.

```text
5/5 PASS
→ PASS

FAIL 1개 이상
→ FAIL

NOT_EVALUATED 1개 이상
→ NOT_EVALUATED

5개 View 미완료
→ Final Result 생성 안 함
```

---

### 2.12 PRE_ROOF Server/FMS Interface

PRE_ROOF는 다음 Transaction 단위를 사용합니다.

```text
1 Request
=
1 inspection_request_id
=
1 inspection_cycle
=
5 Views
=
1 Final Result
```

주요 기능:

- Request Validation
- ACK
- Duplicate 처리
- `REQUEST_CONFLICT`
- Controller-before-ACK
- Transaction Persistence
- Final Result Watcher
- Result Idempotency
- `RESULT_CONFLICT`

Transport Retry와 실제 Reinspection을 별도 Cycle로 구분합니다.

---

### 2.13 PRE_ROOF Unity HMV1 Video

PRE_ROOF Annotated Image도 Global과 동일한 HMV1 Wire Format을 재사용합니다.

주요 기준:

| 항목 | 값 |
|:---|:---|
| Stream ID | `2` |
| Resolution | `1280x720` |
| JPEG Quality | `80` |
| Target FPS | `10` |
| Header | `32 bytes` |
| Payload Max | `1200 bytes` |
| Datagram Max | `1232 bytes` |

Global과 PRE_ROOF는 Wire Format을 공유하면서 Stream ID와 Port Profile을 분리했습니다.

---

### 2.14 Robot / Vision 역할 분리

Vision은 Robot Pose를 직접 제어하지 않습니다.

```text
Robot
→ View Pose 이동

Vision
→ 해당 View 검사
→ Result 생성
```

현재 실제 장비 통합 운용 기준에서는 View Pose를 수동으로 전환합니다.

---

## 3. 공통 기능 설계

### 3.1 상태 기반 검사

Camera Frame이 존재한다는 이유만으로 결과를 바로 생성하지 않습니다.

검사 실행 전 다음 상태를 확인합니다.

- Camera 준비 상태
- Alignment 상태
- Runtime 상태
- Transaction 상태
- `expected_view`

---

### 3.2 통신과 검사 결과 분리

Global과 PRE_ROOF 모두 Request 수신과 실제 검사 완료를 별도 상태로 관리합니다.

```text
ACK
≠ Final Result
```

이를 통해 Network Retry와 실제 재검사를 구분할 수 있습니다.

---

### 3.3 인터페이스 책임 분리

```text
Vision
→ Camera 입력
→ Inspection
→ Result
→ Annotated Video

Server/FMS
→ 공정 상태
→ Request / Result 관리

Unity
→ Digital Twin / Visualization

Robot
→ 실제 View Pose 이동
```

각 시스템의 책임을 분리하고 Vision은 AI Perception Interface에 집중하도록 구성했습니다.

---

### 3.4 Production Validity 보호

Dummy, Local Wire, Communication 검증이 실제 Production Acceptance를 대체하지 않도록 상태를 분리합니다.

```text
production_valid=false
vision_production_valid=false
```

실제 통합 검증 범위와 현재 상태는 [05. 검증](05_validation.md)에 별도로 기록합니다.

---

## 상세 문서

1. [프로젝트 개요](01_overview.md)
2. [시스템 아키텍처](02_architecture.md)
3. [주요 기능](03_features.md)
4. [데이터 흐름](04_data_flow.md)
5. [검증](05_validation.md)
6. [프로젝트 범위](06_project_scope.md)
7. [프로젝트 구조](07_project_structure.md)
