# 05. 검증
AI Perception 검증 결과는 **학습/Validation**, **Runtime 검증**, **Dummy E2E**, **Local Wire E2E**, **Actual Server E2E**, **Actual Unity E2E**, **실제 장비 통합 검증**을 구분해 기록합니다.

단순히 한 번 성공한 로그를 최종 PASS로 취급하지 않고, 실제 검증 범위와 조건이 확인된 항목만 PASS로 표시합니다.

---

<a id="ai-toc-05-01"></a>
## 1. Global Vision 검증

### 1.1 Global Camera

Global Camera는 스마트폰 RTSP 영상을 FFmpeg Direct Pipe로 수신해 ROS2 Image Topic으로 Publish합니다.

검증 항목:

| 항목 | 결과 |
|:---|:---:|
| RTSP 입력 수신 | PASS |
| FFmpeg Direct Pipe Decode | PASS |
| ROS2 Image Publish | PASS |
| Resolution `1280x720` | PASS |
| Encoding `BGR8` | PASS |
| BEST_EFFORT / KEEP_LAST 1 QoS | PASS |
| 일시적 Stream 오류 후 Recovery | PASS |

주요 Topic:

```text
/vision/global_camera/image_raw
```

---

### 1.2 HOUSE B Auto Alignment

HOUSE B는 고정 작업대 구조를 기준으로 ECC Alignment를 수행합니다.

Gate 기준:

| 항목 | 기준 |
|:---|:---:|
| ECC | `>= 0.65` |
| Shift | `<= 60 px` |
| Rotation | `<= 3°` |

Material ROI 내부는 Alignment 계산에서 제외하고 Tape, Board, Background, Corner Marker 등 고정 구조를 Anchor로 사용합니다.

#### 안전 동작 검증

HOUSE B Board가 Reference 위치에서 벗어나거나 작업대에서 제거된 상태에서는 Gate가 Reject되어 Aligned Topic이 생성되지 않는 것을 확인했습니다.

```text
Gate Reject
→ /vision/global_camera/image_aligned 미출력
→ HOUSE B Runtime WAITING
→ 검사 결과 생성 차단
```

이 동작은 잘못된 위치에서 강제로 Inspection을 실행하지 않도록 하는 실패 안전 구조입니다.

---

### 1.3 Incoming QA Runtime

검사 Mode:

```text
BASE A/B
HOUSE B
HOUSE A
```

검증 항목:

| 항목 | 결과 |
|:---|:---:|
| BASE A/B Raw Camera 입력 | PASS |
| HOUSE A Raw Camera 입력 | PASS |
| HOUSE B Aligned Camera 입력 구조 | PASS |
| Final UI Canvas `1280x840` | PASS |
| Camera Area `1280x720` | PASS |
| Annotated Image Publish | PASS |
| HOLD / INSPECT 상태 분리 | PASS |

Annotated Topic:

```text
/vision/incoming_qa/annotated_image
```

검증 Frame:

```text
width=1280
height=720
encoding=bgr8
step=3840
```

---

<a id="ai-toc-05-02"></a>
## 2. Global Server 실제 E2E

Global Incoming Vision은 실제 Team Server와 UDP Request / ACK / Final Result 흐름을 검증했습니다.

검증 구조:

```text
Actual Team Server
→ Vision UDP Request
→ Vision Gateway
→ ACK
→ Vision Result
→ Final Result UDP
→ Actual Team Server
→ Server DB
```

검증 항목:

| 항목 | 결과 |
|:---|:---:|
| Server → Vision Request | PASS |
| Vision ACK | PASS |
| ACK Schema Validation | PASS |
| Vision Transaction DB 기록 | PASS |
| Vision Final Result 송신 | PASS |
| Server Result 수신 | PASS |
| Server DB 반영 | PASS |
| Request / Result Correlation | PASS |

#### 검증 범위

이 검증은 **실제 물리 Inspection 결과 정확도 검증이 아니라 Server Wire Contract와 Transaction E2E 검증**입니다.

통신 검증 Transaction은 물리 추론 없이 Communication-only 상태로 생성했습니다.

따라서 다음 두 결과를 구분합니다.

```text
Server Communication E2E
→ PASS

Actual Physical Inspection Production Scenario
→ 최종 통합 검증 전
```

---

<a id="ai-toc-05-03"></a>
## 3. Global Unity 실제 E2E

Incoming Annotated Image를 HMV1 UDP로 실제 Unity에 전달했습니다.

검증 구조:

```text
/vision/incoming_qa/annotated_image
→ HMV1 Sender
→ Vision NIC
→ UDP
→ Actual Unity Receiver
→ Unity 화면 표시
```

검증 항목:

| 항목 | 결과 |
|:---|:---:|
| ROS2 Annotated Publisher | PASS |
| HMV1 Version 1 | PASS |
| Stream ID 1 | PASS |
| Header 32 bytes | PASS |
| JPEG Quality 80 | PASS |
| Payload `<=1200 bytes` | PASS |
| Datagram `<=1232 bytes` | PASS |
| Vision NIC UDP 송출 | PASS |
| Unity UDP 수신 | PASS |
| Unity 영상 표시 | PASS |

실제 Packet Capture에서 UDP Datagram이 정상 송출되고 Kernel Drop 없이 수신되는 것을 확인했습니다.

---

<a id="ai-toc-05-04"></a>
## 4. Global Model / Dataset 검증

Incoming Runtime은 데이터 보강과 재학습을 누적한 계보를 사용합니다.

Runtime 계보:

```text
Attempt02
→ Attempt03
→ Attempt04
→ Attempt05
→ Attempt06
```

Attempt06 기준:

| 항목 | 값 |
|:---|:---|
| Dataset Total | `25,200` |
| Train | `24,780` |
| Validation | `420` |
| Best Epoch | `14` |
| Validation Loss | `0.0007523952257815701` |

이 수치는 학습 / Validation 결과이며, 실제 전체 공정 정확도 또는 Production Acceptance Rate와 동일한 지표로 해석하지 않습니다.

모델 Weight와 대용량 Dataset은 공개 저장소에 포함하지 않고 Runtime 코드와 Contract / Recipe만 공개합니다.

---

<a id="ai-toc-05-05"></a>
## 5. Depth Vision 입력 검증

Intel RealSense D435는 Robot Control PC에 연결되어 있으며 Vision PC는 네트워크 Endpoint를 통해 RGB / Depth Image를 수신합니다.

실제 입력 검증:

| 항목 | 결과 |
|:---|:---:|
| RGB Endpoint 응답 | PASS |
| RGB JPEG Decode | PASS |
| RGB Resolution `1280x720` | PASS |
| Depth Image Endpoint 응답 | PASS |
| Depth JPEG Decode | PASS |
| Depth Resolution `1280x720` | PASS |

현재 Depth 입력은 JPEG 시각화 Image이며 metric 16-bit Raw Depth와 동일하게 취급하지 않습니다.

---

<a id="ai-toc-05-06"></a>
## 6. PRE_ROOF 5-View 개별 Runtime 검증

최종 View Runtime:

| View | Runtime | 상태 |
|:---|:---:|:---:|
| TOP | V5 | PASS |
| LEFT | V3 | PASS |
| RIGHT | V7 | PASS |
| FRONT | V1 | PASS |
| BEHIND | V4 | PASS |

아래 PASS는 각 View의 개별 Runtime 기준이며, D435 + Robot을 포함한 5-View Actual E2E PASS를 의미하지 않습니다.

각 View Runtime은 독립적인 Golden / ROI / Threshold / Metric Profile을 사용합니다.

최종 View Result:

```text
PASS
FAIL
NOT_EVALUATED
```

---

<a id="ai-toc-05-07"></a>
## 7. PRE_ROOF Integration Controller V3 Self-Test

Controller V3 Self-Test에서 다음 상태 전이를 확인했습니다.

| 검증 항목 | 결과 |
|:---|:---:|
| Request → HOLD / TOP | PASS |
| STARTING 상태 Block | PASS |
| TOP → LEFT | PASS |
| LEFT → RIGHT | PASS |
| Runtime ERROR → NOT_EVALUATED | PASS |
| FRONT → BEHIND | PASS |
| BEHIND → Final Ready | PASS |
| Final Wire Payload | PASS |
| Reinspection New Cycle | PASS |
| Self-Test 중 Persistent State 비변경 | PASS |

Controller는 Runtime Offline 또는 Result Not Ready 상태에서 View Commit을 차단합니다.

---

<a id="ai-toc-05-08"></a>
## 8. PRE_ROOF Dummy Full E2E

Actual Server 연결 전 Vision 측 전체 Transaction 경로를 Dummy Harness로 검증했습니다.

검증 구조:

```text
Dummy Server Request
→ UDP Gateway V3
→ Controller V3 Dummy Harness
→ TOP PASS
→ LEFT PASS
→ RIGHT PASS
→ FRONT PASS
→ BEHIND PASS
→ Overall PASS
→ Final Result Ready
→ Gateway Result Watcher
→ Dummy Result Receiver
```

검증 항목:

| 항목 | 결과 |
|:---|:---:|
| Request UDP | PASS |
| ACK | PASS |
| TOP | PASS |
| LEFT | PASS |
| RIGHT | PASS |
| FRONT | PASS |
| BEHIND | PASS |
| Overall | PASS |
| Final Result UDP | PASS |
| Result Correlation | PASS |
| Controller Provenance V3 | PASS |

#### 검증 범위

이 검증에서는 실제 D435와 Robot을 사용하지 않았습니다.

```text
D435 NOT USED
Robot NOT USED
```

따라서 **Server Pre-Integration PASS**이며 Actual Production E2E와 구분합니다.

---

<a id="ai-toc-05-09"></a>
## 9. PRE_ROOF HMV1 Local Wire E2E

Actual Unity 연결 전 HMV1 Wire Format을 Local Receiver로 검증했습니다.

검증 구조:

```text
Dummy ROS Image 1280x720 BGR8
→ /vision/pre_roof/annotated_image
→ HMV1 Sender V2
→ stream_id=2
→ UDP :21020
→ Dummy Unity Receiver
→ JPEG Reassembly
→ JPEG Decode
```

검증 결과:

| 항목 | 결과 |
|:---|:---:|
| ROS2 Annotated Topic | PASS |
| Width `1280` | PASS |
| Height `720` | PASS |
| Encoding `bgr8` | PASS |
| Step `3840` | PASS |
| HMV1 Version 1 | PASS |
| Stream ID 2 | PASS |
| Header 32 bytes | PASS |
| Payload `<=1200` | PASS |
| Datagram `<=1232` | PASS |
| JPEG Reassembly | PASS |
| JPEG Decode | PASS |
| Decoded Frame `1280x720` | PASS |

이 검증 역시 Actual Unity 연결 전 **Local Wire E2E**입니다.

---

<a id="ai-toc-05-10"></a>
## 10. 현재 Actual Integration 상태

### Global Vision

| 항목 | 상태 |
|:---|:---:|
| Global Camera | PASS |
| Incoming Runtime | PASS |
| Actual Team Server Communication E2E | PASS |
| Actual Unity HMV1 E2E | PASS |
| 전체 실제 물리검사 Production Scenario | 최종 통합 검증 전 |

### Depth Vision / PRE_ROOF

| 항목 | 상태 |
|:---|:---:|
| D435 RGB / Depth 입력 | PASS |
| 5-View 개별 Runtime 기준 | PASS |
| Controller V3 Self-Test | PASS |
| Dummy Server Full E2E | PASS |
| Local HMV1 Wire E2E | PASS |
| Actual Team Server PRE_ROOF | 최종 통합 검증 전 |
| Actual Unity PRE_ROOF | 최종 통합 검증 전 |
| D435 + Manual Robot 5-View Actual E2E | 최종 통합 검증 전 |

---

<a id="ai-toc-05-11"></a>
## 11. Production Validity 기준

현재 최종 실제 통합 검증이 완료되지 않은 항목은 Production PASS로 표현하지 않습니다.

```text
Global Server Communication
→ Actual E2E PASS

Global Unity Video
→ Actual E2E PASS

PRE_ROOF Server Path
→ Dummy E2E PASS

PRE_ROOF Unity Path
→ Local Wire E2E PASS

PRE_ROOF Actual Server / Unity / D435 + Robot
→ 최종 실제 통합 검증 전
```

따라서 실제 인수 검증 완료 전까지 다음 상태를 유지합니다.

```text
production_valid=false
vision_production_valid=false
```

---

<a id="ai-toc-05-12"></a>
## 12. 검증 결과 해석 원칙

이 프로젝트에서는 다음 결과를 서로 대체하지 않습니다.

```text
Synthetic / Validation 성능
≠ 실제 장비 정확도

Dummy E2E
≠ Actual Server E2E

Local Wire E2E
≠ Actual Unity E2E

Communication E2E
≠ Physical Inspection Production PASS
```

각 PASS는 해당 검증 범위에서만 유효하며, 최종 문서에서도 동일한 기준으로 구분해 기록합니다.

---

## 목차

- [1. Global Vision 검증](#ai-toc-05-01)
- [2. Global Server 실제 E2E](#ai-toc-05-02)
- [3. Global Unity 실제 E2E](#ai-toc-05-03)
- [4. Global Model / Dataset 검증](#ai-toc-05-04)
- [5. Depth Vision 입력 검증](#ai-toc-05-05)
- [6. PRE_ROOF 5-View 개별 Runtime 검증](#ai-toc-05-06)
- [7. PRE_ROOF Integration Controller V3 Self-Test](#ai-toc-05-07)
- [8. PRE_ROOF Dummy Full E2E](#ai-toc-05-08)
- [9. PRE_ROOF HMV1 Local Wire E2E](#ai-toc-05-09)
- [10. 현재 Actual Integration 상태](#ai-toc-05-10)
- [11. Production Validity 기준](#ai-toc-05-11)
- [12. 검증 결과 해석 원칙](#ai-toc-05-12)
