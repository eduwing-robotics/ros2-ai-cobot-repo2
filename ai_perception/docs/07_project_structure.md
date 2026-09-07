# 07. 프로젝트 구조
이 디렉터리는 팀 통합 저장소에서 **AI Perception / Vision 담당 구현을 선별해 정리한 영역**입니다.

개발 Workspace 전체를 복사하지 않고, 최종 Runtime과 Interface를 이해하는 데 필요한 코드·설정·문서만 포함합니다.

---

<a id="ai-toc-07-01"></a>
## 1. 전체 구조

```text
ai_perception/
├─ README.md
├─ config/
│  └─ global_vision/
├─ scripts/
│  ├─ global_vision/
│  │  ├─ global_camera_ros2/
│  │  ├─ incoming_inspection/
│  │  ├─ incoming_qa_runtime/
│  │  └─ network/
│  └─ depth_vision/
│     ├─ pre_roof_runtime/
│     ├─ integration/
│     └─ network/
└─ docs/
   ├─ README.md
   ├─ 01_overview.md
   ├─ 02_architecture.md
   ├─ 03_features.md
   ├─ 04_data_flow.md
   ├─ 05_validation.md
   ├─ 06_project_scope.md
   └─ 07_project_structure.md
```

---

<a id="ai-toc-07-02"></a>
## 2. Global Vision

### 2.1 Global Camera ROS2

경로:

```text
scripts/global_vision/global_camera_ros2/
```

구성:

```text
harmony_global_camera/
├─ __init__.py
└─ global_camera_node.py

resource/
└─ harmony_global_camera

package.xml
setup.cfg
setup.py
```

`global_camera_node.py`는 스마트폰 RTSP 영상을 FFmpeg Direct Pipe로 받아 ROS2 Image Topic으로 변환합니다.

주요 Topic:

```text
/vision/global_camera/image_raw
```

---

### 2.2 Incoming Inspection

경로:

```text
scripts/global_vision/incoming_inspection/
```

주요 파일:

```text
harmony_incoming_inspection_final_v1.py
harmony_incoming_inspection_final_v2.py
harmony_incoming_inspection_final_v3.py
harmony_incoming_inspection_final_v4_ui.py
harmony_unified_runtime_attempt06_house_b_v2.py
house_b_global_auto_align_headless_v1.py
```

Final Runtime 계보:

```text
V1
→ V2
→ V3
→ V4 UI
```

역할:

| 파일 | 핵심 역할 |
|:---|:---|
| `final_v1` | Incoming Inspection 기본 Runtime |
| `final_v2` | Server Transaction Integration |
| `final_v3` | Unity Annotated Image Topic |
| `final_v4_ui` | UI Layer 개선 |
| `unified_runtime_attempt06` | AI / Inspection Runtime |
| `auto_align_headless_v1` | HOUSE B Auto Alignment |

---

### 2.3 Incoming Server Runtime

경로:

```text
scripts/global_vision/incoming_qa_runtime/
```

구성:

```text
contracts_v2.py
transaction_store_v2.py
udp_gateway_v2.py
```

각 파일의 책임:

| 파일 | 역할 |
|:---|:---|
| `contracts_v2.py` | Request / ACK / Result Contract |
| `transaction_store_v2.py` | SQLite Transaction Persistence |
| `udp_gateway_v2.py` | Server UDP Request / ACK / Result 연동 |

---

### 2.4 Global Unity Network

경로:

```text
scripts/global_vision/network/
```

파일:

```text
harmony_unity_video_udp_v1.py
```

주요 Profile:

```text
stream_id=1
topic=/vision/incoming_qa/annotated_image
```

Incoming Annotated Image를 HMV1 UDP Video로 변환해 Unity에 전달합니다.

---

<a id="ai-toc-07-03"></a>
## 3. Global Vision Config

경로:

```text
config/global_vision/
```

구성:

```text
harmony_unified_inspection_recipe_v2.json
harmony_unified_model_contract_v1.json
```

역할:

| 파일 | 역할 |
|:---|:---|
| Inspection Recipe | Runtime 검사 조건과 운영 설정 연결 |
| Model Contract | Runtime Model 계보 및 Class Contract 관리 |

실제 Model Weight는 공개 소스에서 분리합니다.

---

<a id="ai-toc-07-04"></a>
## 4. Depth Vision

### 4.1 PRE_ROOF 5-View Runtime

경로:

```text
scripts/depth_vision/pre_roof_runtime/
```

구성:

```text
harmony_pre_roof_qc_top_runtime_v5.py
harmony_pre_roof_qc_left_runtime_v3.py
harmony_pre_roof_qc_right_runtime_v7.py
harmony_pre_roof_qc_front_runtime_v1.py
harmony_pre_roof_qc_behind_runtime_v4.py
harmony_pre_roof_qc_5view_dashboard_v17.py
```

View별 최종 Runtime:

| View | Runtime |
|:---|:---:|
| TOP | V5 |
| LEFT | V3 |
| RIGHT | V7 |
| FRONT | V1 |
| BEHIND | V4 |

각 Runtime은 View별 Golden / ROI / Threshold / Metric Profile을 사용합니다.

Dashboard V17은 5-View 상태를 표시하는 Visual Layer이며, 상태 전이 Authority는 Controller V3가 담당합니다.

---

### 4.2 PRE_ROOF Integration

경로:

```text
scripts/depth_vision/integration/
```

구성:

```text
harmony_pre_roof_integration_controller_v3.py
contracts_v1.py
udp_gateway_v2.py
udp_gateway_v3.py
```

역할:

| 파일 | 핵심 역할 |
|:---|:---|
| `integration_controller_v3` | 5-View State / Overall / Final Result 관리 |
| `contracts_v1.py` | PRE_ROOF Server Wire Contract |
| `udp_gateway_v2.py` | Gateway 기반 기능 |
| `udp_gateway_v3.py` | Controller V3 / Final Result 연동 |

Controller V3는 다음 상태를 관리합니다.

- Request Correlation
- `inspection_cycle`
- `expected_view`
- View Result Normalize
- View Commit
- Overall
- Final Result Ready
- Reinspection
- Persistent State

---

### 4.3 PRE_ROOF Unity Network

경로:

```text
scripts/depth_vision/network/
```

파일:

```text
harmony_unity_video_udp_v2.py
```

주요 Profile:

```text
stream_id=2
topic=/vision/pre_roof/annotated_image
```

Global HMV1 Wire Format을 재사용하고 PRE_ROOF Stream을 별도로 구분합니다.

---

<a id="ai-toc-07-05"></a>
## 5. Runtime 외부 의존 자산

공개한 Runtime 중 일부는 개발 Workspace의 별도 Asset을 참조합니다.

### Global Vision

대표 의존 자산:

- Model Weight
- ROI Contract
- HOUSE B Reference Image
- Validation / Runtime Asset

### Depth Vision

대표 의존 자산:

- Golden RGB / Depth
- ROI JSON
- Threshold JSON
- Lift Threshold
- Base Hole Config
- Structure Threshold
- Active View 관련 Runtime Asset

따라서 이 저장소는 개발환경 전체를 그대로 복제한 **완전한 배포 패키지**가 아니라, 핵심 구현과 Interface를 검토할 수 있도록 선별한 소스 구조입니다.

---

<a id="ai-toc-07-06"></a>
## 6. 공개 저장소 포함 범위

포함:

- Global Camera ROS2 Package
- Incoming Final Runtime Chain
- Unified Inspection Runtime
- HOUSE B Auto Alignment
- Server Contract / Gateway / Transaction Store
- Global HMV1 Sender
- Inspection Recipe / Model Contract
- PRE_ROOF 5-View Runtime
- Dashboard V17
- Integration Controller V3
- PRE_ROOF Contract / Gateway
- PRE_ROOF HMV1 Sender
- 기술 문서

---

<a id="ai-toc-07-07"></a>
## 7. 공개 저장소 제외 범위

다음 항목은 공개 소스에서 제외합니다.

```text
Raw Dataset
Synthetic Dataset 전체
Model Weight
Training Cache
Runtime SQLite DB
대량 Capture / Audit
Probe
Canary
Legacy Runtime 전체
Backup
__pycache__
.pyc
.env
API Key / Token / Password
개인 환경 파일
```

대용량 데이터와 실험 산출물 전체를 노출하기보다 최종 구조와 검증 근거를 중심으로 정리합니다.

---

<a id="ai-toc-07-08"></a>
## 8. 개발 Workspace와 저장소 분리

개발 Workspace와 팀 Git 저장소는 분리해 운영합니다.

```text
Development Workspace
→ 실제 개발·검증 원본 보존

Team Git Repository
→ 검토된 최종 코드만 ai_perception/에 선별 반영
```

개발 원본을 직접 정리하거나 삭제하지 않고, 공개·공유에 필요한 최종 구현만 별도 저장소 구조에 반영합니다.

---

<a id="ai-toc-07-09"></a>
## 9. 문서 구조

```text
README.md
→ 핵심 요약

docs/01_overview.md
→ 프로젝트 개요

docs/02_architecture.md
→ 시스템 구조

docs/03_features.md
→ 주요 구현 기능

docs/04_data_flow.md
→ Runtime 데이터 흐름

docs/05_validation.md
→ 검증 결과

docs/06_project_scope.md
→ 담당 범위와 책임 경계

docs/07_project_structure.md
→ 공개 소스 구조
```

대표 이미지와 영상은 별도 미디어 문서로 분리하지 않고 관련 기능 또는 검증 문서에 직접 배치하는 방식을 사용합니다.

---

<a id="ai-toc-07-10"></a>
## 10. 구조 설계 목적

이 구조는 다음 내용을 빠르게 확인할 수 있도록 구성했습니다.

```text
무엇을 만들었는가?
→ AI Perception / Vision

어떤 코드가 핵심인가?
→ Runtime / Controller / Gateway / Network Interface

팀 시스템과 어디서 연결되는가?
→ Server / Unity / Robot Interface

어디까지 직접 담당했는가?
→ Global Vision + Depth Vision

무엇을 검증했는가?
→ docs/05_validation.md
```

팀 전체 시스템을 개인 구현으로 표현하지 않고, 실제 Vision 담당 범위와 구현 근거를 명확히 보여주는 것이 목적입니다.

---

## 목차

- [1. 전체 구조](#ai-toc-07-01)
- [2. Global Vision](#ai-toc-07-02)
- [3. Global Vision Config](#ai-toc-07-03)
- [4. Depth Vision](#ai-toc-07-04)
- [5. Runtime 외부 의존 자산](#ai-toc-07-05)
- [6. 공개 저장소 포함 범위](#ai-toc-07-06)
- [7. 공개 저장소 제외 범위](#ai-toc-07-07)
- [8. 개발 Workspace와 저장소 분리](#ai-toc-07-08)
- [9. 문서 구조](#ai-toc-07-09)
- [10. 구조 설계 목적](#ai-toc-07-10)
