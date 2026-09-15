# Voice AI 프로젝트 구조와 모듈 경계

이 문서는 Voice AI의 문서 진입점과 실제 Runtime source 위치가 다른 이유를 설명합니다.

## 현재 구조

```text
llm_stt_tts/
├── README.md                 # Voice AI 기능 소개·발화 예시
└── docs/                     # 상세 기술 문서

server_fms_db/
├── voice_runtime/            # microphone, Wake Word, VAD, session, playback
├── api_server/               # Voice API, STT, interpreter, conversation, TTS
├── shared/                   # production·inventory·PROCESS authority
├── fms_server/               # orchestration worker
├── benchmarks/               # benchmark harness
└── experiments/wakeword_sherpa/ # Wake Word experiment source
```

`llm_stt_tts/`는 Voice AI 기능을 설명하는 문서 진입점입니다. 실제 실행 가능한 source는 `server_fms_db/`에 통합되어 있습니다. 같은 source를 두 폴더에 복제하지 않고 하나의 source of truth를 유지합니다.

## 통합 배치 이유

Voice 기능을 별도 폴더로 물리 분리하지 않은 이유는 실제 의존 관계 때문입니다.

- Voice Runtime은 shared Redis realtime contract와 production monitoring readback을 사용합니다.
- Voice API route는 API Server 내부에 있으며 STT·LLM·TTS service를 직접 조합합니다.
- `ProductionConversationService`는 production request, inventory preflight, Job materialization을 사용합니다.
- Voice tests는 `api_server`, `shared`, `voice_runtime` fixture를 함께 사용합니다.
- Voice 상태 조회와 자동 TTS는 Server/FMS의 canonical PROCESS projection을 authority로 사용합니다.

따라서 문서 영역과 Runtime source 영역을 구분하되, import 경계를 깨뜨릴 수 있는 복제·강제 이동은 하지 않습니다.

## 실제 source map

| 영역 | 실제 source |
| --- | --- |
| Voice Runtime | [`server_fms_db/voice_runtime/`](../../server_fms_db/voice_runtime/) |
| Voice API | [`api_server/routers/ai.py`](../../server_fms_db/api_server/routers/ai.py) |
| STT | [`api_server/services/stt_service.py`](../../server_fms_db/api_server/services/stt_service.py) |
| Command Interpreter | [`api_server/services/command_interpreter.py`](../../server_fms_db/api_server/services/command_interpreter.py) |
| Ollama fallback | [`api_server/services/llm_service.py`](../../server_fms_db/api_server/services/llm_service.py) |
| Production Conversation | [`api_server/services/production_conversation_service.py`](../../server_fms_db/api_server/services/production_conversation_service.py) |
| Response message | [`api_server/services/response_message_builder.py`](../../server_fms_db/api_server/services/response_message_builder.py) |
| TTS | [`api_server/services/tts/`](../../server_fms_db/api_server/services/tts/) |
| Voice observability | [`api_server/services/voice_runtime_monitor.py`](../../server_fms_db/api_server/services/voice_runtime_monitor.py) |
| Wake Word experiments | [`experiments/wakeword_sherpa/`](../../server_fms_db/experiments/wakeword_sherpa/) |
| Benchmark harness | [`benchmarks/`](../../server_fms_db/benchmarks/) |
| Related tests | [`tests/`](../../server_fms_db/tests/) |

## 책임 경계

| Voice AI가 담당하는 것 | Server/FMS가 authority를 가지는 것 |
| --- | --- |
| Wake Word, microphone capture, Energy VAD | Production Job, JobStep, inventory |
| STT, command interpretation, production conversation interface | Robot execution, material transport, inspection lifecycle |
| TTS, speaker playback, 자동 공정 안내 | durable state, execution history, canonical PROCESS projection |
| API 응답을 음성 session에 연결 | Job 생성·취소·pause/resume의 업무 규칙 |

Voice는 생산 상태를 자체적으로 생성하거나 다음 공정을 결정하지 않습니다. 생산 상태와 실행 결정은 Server/FMS의 durable lifecycle을 따르고, Voice는 API 응답과 PROCESS projection을 사용합니다.

## 공개 범위와 제외 범위

| 공개 포함 | 공개 제외 |
| --- | --- |
| source, test, benchmark harness, experiment source | `.env`, credential, API key, private certificate |
| 안전한 config template, documentation | PostgreSQL/Redis dump, runtime DB, backup |
| benchmark manifest 등 작은 재현 보조 파일 | 대용량 model weight, 원본 capture/dataset, recorded audio |
|  | synthesized audio artifact, benchmark generated result, log, cache, virtualenv |

이 구분은 root `.gitignore`와 server module의 공개용 template 정책을 따릅니다. 재현에 필요한 source·설정 이름·작은 manifest는 포함할 수 있지만, 비밀값·대용량 산출물·개인 실행 환경 파일은 포함하지 않습니다.

## 문서의 역할 분리

- [상위 README](../README.md): 무엇을 만들었는지, 핵심 pipeline과 사용자 관점 기능
- [Architecture](01_architecture.md): 실제 Runtime·API·FMS 연결과 데이터 흐름
- [Troubleshooting & Validation](02_troubleshooting_validation.md): 문제 분석·개선·검증 범위
- 이 문서: source가 어디에 있고 왜 그 경계를 유지하는지
