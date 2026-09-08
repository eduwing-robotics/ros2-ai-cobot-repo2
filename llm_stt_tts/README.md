# LLM / STT / TTS

음성으로 공정에 지시하고 상태를 안내받는 음성 인터페이스입니다. STT로 음성을 문자로 변환하고, LLM이 의도를 해석해 명령으로 바꾸며, TTS로 결과를 음성 안내합니다.

---

## 프로젝트 정보

| 항목 | 내용 |
|:---|:---|
| 프로젝트 | AI 기반 조립식 주택 자동화 공장 |
| 형태 | 팀 프로젝트 |
| 담당 영역 | LLM / STT / TTS |
| 개발 환경 | 작성 예정 |
| 주요 기술 | 작성 예정 |

---

## 담당 범위

- 음성 입력 및 STT 변환
- LLM 기반 의도 해석 및 명령 매핑
- 공정 상태 음성 안내 (TTS)
- 상위 시스템 명령 전달 Interface

세부 항목은 담당자가 확정 후 작성합니다.

---

## 처리 흐름

```text
Mic
→ STT
→ LLM 의도 해석
→ 명령 매핑
→ Team Server / Control Tower
→ 결과 응답
→ TTS
```

---

## 시스템 연동

| 대상 | Interface |
|:---|:---|
| Team Server / FMS | 작성 예정 |
| Control Tower GUI | 작성 예정 |

---

## 저장소 구조

```text
llm_stt_tts/
├─ README.md
├─ src/
├─ config/
└─ docs/
   └─ README.md
```

API Key와 모델 가중치는 저장소에 포함하지 않습니다. Key 이름은 `.env.example`로 공유합니다.

---

## 상세 문서

[문서 목록](docs/README.md)
