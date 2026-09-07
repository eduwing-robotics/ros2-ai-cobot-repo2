# 01. 프로젝트 개요

## 프로젝트 배경

조립식 주택 자동화 공정에서는 자재가 올바르게 투입되었는지 확인하는 **입고 검사**와, 로봇 조립 이후 구조물이 정상적으로 조립되었는지 확인하는 **조립 품질 검사**가 필요합니다.

이 프로젝트의 AI Perception 시스템은 하나의 Vision 방식으로 모든 검사를 처리하지 않고, 검사 거리와 목적에 따라 다음 두 영역으로 분리했습니다.

- **Global Vision**: 공정 전체를 넓게 관찰하고 자재 입고 상태를 검사
- **Depth Vision**: Intel RealSense D435를 이용해 근접 조립 상태를 PRE_ROOF 5-View로 검사

---

## 문제 정의

### Global Vision

Global Camera는 네트워크 기반 RTSP 입력을 사용하고, 동일한 Camera에서 여러 검사 Mode를 처리해야 합니다.

주요 문제는 다음과 같습니다.

- RTSP 네트워크 입력의 지연과 일시적인 Frame 오류
- ROS2 Runtime에서 사용할 해상도와 Encoding 고정
- HOUSE B 작업대 위치 편차에 따른 검사 영역 불일치
- BASE A/B, HOUSE A, HOUSE B 등 서로 다른 검사 Mode 관리
- Server Request와 Vision 검사 상태의 Transaction 동기화
- 검사 UI와 Unity Annotated Video를 동시에 유지해야 하는 요구

### Depth Vision

PRE_ROOF 조립 품질 검사는 하나의 Camera 방향만으로 전체 구조를 확인하기 어렵습니다.

따라서 다음 문제를 해결해야 했습니다.

- TOP / LEFT / RIGHT / FRONT / BEHIND별 가시 영역 차이
- View별 다른 Golden / ROI / Threshold / Metric 필요
- 독립적인 View Runtime 결과를 하나의 Inspection Cycle로 통합
- Robot 이동 또는 Runtime 준비 전 검사 실행 방지
- Runtime Offline, Result Not Ready 상태에서 잘못된 View Commit 방지
- 5개 View가 모두 완료되기 전 Final Result 생성 차단

---

## 개발 목적

AI Perception 시스템의 개발 목적은 다음과 같습니다.

1. Global Camera와 D435 입력을 안정적인 Runtime 입력으로 변환한다.
2. 검사 목적에 따라 Global Vision과 Depth Vision을 분리한다.
3. Vision 결과를 PASS / FAIL / NOT_EVALUATED 등 명확한 상태로 관리한다.
4. Team Server/FMS와 Request / ACK / Final Result 단위로 연동한다.
5. Unity에는 Vision Annotated Video를 별도 Stream으로 전달한다.
6. 실제 검증이 완료되지 않은 상태를 Production PASS로 오인하지 않도록 한다.

---

## 전체 시스템에서의 역할

전체 팀 프로젝트는 다음 영역으로 구성됩니다.

```text
AI 기반 조립식 주택 자동화 공장
├─ TurtleBot / Logistics
├─ FR5 / Robot Operation
├─ Team Server / FMS
├─ Unity / Digital Twin
└─ AI Perception / Vision
   ├─ Global Vision
   └─ Depth Vision
```

이 문서에서 다루는 범위는 **AI Perception / Vision**입니다.

Vision은 Camera 입력, 영상 처리, 검사, 결과 생성, Annotated Video 생성을 담당합니다.

반면 다음 영역은 별도 담당 시스템입니다.

- Robot Motion Planning 및 Manipulation
- TurtleBot Navigation
- Team Server/FMS 내부 Business Logic
- Unity Digital Twin 내부 Scene 및 공정 상태 로직

Vision은 이 시스템들과의 **Interface 설계 및 연동 검증**을 담당합니다.

---

## Global Vision 개요

Global Vision은 스마트폰 RTSP Camera를 이용해 공정 영역을 관찰하고 Incoming QA를 수행합니다.

기본 흐름은 다음과 같습니다.

```text
RTSP Camera
→ FFmpeg Low-Latency Direct Pipe
→ ROS2 Global Image
→ Raw / Aligned Camera Source
→ Incoming QA
→ Inspection Result
→ Team Server / Unity
```

주요 구현은 다음과 같습니다.

- Global Camera ROS2 Node
- `/vision/global_camera/image_raw`
- HOUSE B Auto Alignment
- `/vision/global_camera/image_aligned`
- BASE A/B / HOUSE B / HOUSE A Incoming QA
- Incoming Inspection UI
- `/vision/incoming_qa/annotated_image`
- Server UDP Gateway
- SQLite Transaction Store
- Unity HMV1 `stream_id=1`

---

## Depth Vision 개요

Depth Vision은 Robot Control PC에 연결된 Intel RealSense D435의 RGB / Depth Image를 이용해 PRE_ROOF 조립 상태를 검사합니다.

검사 순서는 다음과 같습니다.

```text
TOP
→ LEFT
→ RIGHT
→ FRONT
→ BEHIND
→ Overall
→ Final Result
```

각 View는 독립적인 검사 Profile을 사용합니다.

- Golden
- ROI
- Threshold
- Metric

Integration Controller가 View 순서, Inspection Cycle, View Commit, Overall Result와 Final Result 생성을 관리합니다.

---

## 핵심 설계 방향

### 1. Global Vision과 Depth Vision 분리

Global Vision과 Depth Vision은 입력 장비, 검사 거리, 검사 목적이 다르기 때문에 하나의 Runtime에 합치지 않고 독립적으로 구성했습니다.

### 2. Raw Frame과 Aligned Frame 분리

Global Incoming에서 모든 Mode가 같은 Camera Source를 사용하지 않습니다.

- BASE A/B: Raw
- HOUSE A: Raw
- HOUSE B: Aligned

HOUSE B는 Alignment Gate를 통과한 Frame만 검사에 사용합니다.

### 3. 검사와 공정 상태의 책임 분리

Vision은 검사 결과를 생성하지만 공정 전체 상태의 Source of Truth는 Team Server/FMS가 담당합니다.

ACK 또한 검사 완료가 아니라 **Request 수신 및 Transaction 수락**을 의미합니다.

### 4. View Runtime과 Integration State Machine 분리

PRE_ROOF의 각 View Runtime은 실제 영상 검사에 집중하고, 전체 상태 전이는 Integration Controller가 관리합니다.

이를 통해 View별 검사 Logic과 전체 Inspection Cycle Logic을 분리했습니다.

### 5. 실패 안전 구조

다음 상태에서는 결과를 강제로 생성하지 않습니다.

- Camera Frame 미수신
- HOUSE B Alignment Gate Reject
- PRE_ROOF Runtime Offline
- Result Not Ready
- 5-View 미완료

### 6. 검증 단계 구분

다음 검증 결과를 하나의 최종 성능으로 합치지 않습니다.

- 학습 / Validation
- Runtime 검증
- Dummy E2E
- Local Wire E2E
- Actual Server E2E
- Actual Unity E2E
- 실제 D435 + Robot E2E

---

## 주요 구현 결과

### Global Vision

- RTSP → FFmpeg → ROS2 Image Pipeline 구현
- Global Camera `1280x720 BGR8` Runtime 구성
- HOUSE B Auto Alignment 및 Gate 구현
- BASE A/B / HOUSE B / HOUSE A Incoming QA Runtime 구현
- Incoming UI와 Unity Annotated Image Frame 분리
- Team Server Request / ACK / Final Result UDP Interface 구현
- SQLite 기반 Transaction 관리
- HMV1 `stream_id=1` Unity Video Interface 구현
- 실제 Team Server 통신 E2E 검증
- 실제 Unity Annotated Video E2E 검증

### Depth Vision

- D435 RGB `/raw` `1280x720` 입력 확인
- D435 Depth Image `/depthimg` `1280x720` 입력 확인
- TOP / LEFT / RIGHT / FRONT / BEHIND 5-View Runtime 구현
- 5-View Dashboard 구현
- Integration Controller V3 구현
- Server Wire Contract 및 UDP Gateway 구현
- Dummy Server 기반 5-View Full E2E 검증
- HMV1 `stream_id=2` Local Wire E2E 검증

---

## 현재 검증 상태

Global Vision에서는 실제 Team Server 통신과 실제 Unity HMV1 영상 수신까지 확인했습니다.

다만 Global Incoming의 **전체 실제 물리검사 Production Scenario 최종 통합 검증**은 별도 단계로 남아 있습니다.

PRE_ROOF는 Vision 측 Pre-Integration 단계에서 다음 항목을 검증했습니다.

- Controller V3 Self-Test
- Dummy Server Request / ACK
- Dummy TOP → BEHIND 5-View Full E2E
- Overall / Final Result UDP
- HMV1 `stream_id=2` Local Wire E2E

다음 항목은 아직 최종 실제 통합 검증 전입니다.

- Actual Team Server PRE_ROOF
- Actual Unity PRE_ROOF
- D435 + Manual Robot 5-View Actual E2E

따라서 완료되지 않은 항목은 Production PASS로 표현하지 않습니다.
