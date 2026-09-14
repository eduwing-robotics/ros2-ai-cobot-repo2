# Server / FMS / DB

AI 기반 조립식 주택 자동화 공장의 **통합 backend** 모듈입니다. API Server, FMS, Telemetry Gateway, PostgreSQL 기반 durable state, Redis realtime event, Voice Runtime을 하나의 Python runtime으로 구성합니다.

> Voice Runtime은 `shared` Redis event contract 및 API Server의 `/ai` endpoint에 직접 의존합니다. 현재는 실행 안정성을 위해 이 통합 backend 안에 함께 배치합니다. 기존 `llm_stt_tts/` 모듈은 팀 공용 Voice 문서 scaffold로 보존합니다.

## 1. 담당 범위

- FastAPI API Server와 Production Monitor
- FMS production orchestration 및 Recipe-driven Job/JobStep lifecycle
- PostgreSQL · SQLAlchemy 모델, Alembic migration, 재고·물류·검사 durable state
- Redis 기반 production/transport/inspection realtime event
- ROS 2 Robot Cell · TurtleBot Action adapter 및 Telemetry Gateway
- Vision 수입검사·조립 결과 품질검사 UDP interface
- Unity WebSocket production snapshot/realtime status
- 음성 명령 API(STT · LLM intent interpretation · TTS) 및 Voice Runtime

## 2. 구성

| 구성 요소 | 실제 패키지 | 역할 |
| --- | --- | --- |
| API Server | `api_server/` | HTTP API, 음성 요청, production read model, Unity WebSocket, 운영 UI |
| FMS | `fms_server/` | 실행 조건 판단, robot/transport dispatch, 결과 처리, 검사 orchestration |
| Shared domain | `shared/` | 설정, SQLAlchemy 모델, schema, domain service, Redis contract |
| Telemetry Gateway | `telemetry_gateway/` | ROS 2 telemetry 수집과 Redis latest state 전달 |
| Voice Runtime | `voice_runtime/` | Wake Word, VAD, API 대화 요청, TTS 재생, 생산 공정 음성 안내 |
| Migration | `alembic/` | PostgreSQL schema version 관리 |
| Tests | `tests/` | unit, API, fake transport 및 integration marker regression |

## 3. Production orchestration

Production Job은 Recipe snapshot의 `JobStep` frontier를 기준으로 진행합니다. 자재 delivery, DROP ownership, 수입검사, 조립 결과 품질검사, 로봇 실행 Attempt와 재고 소비는 각각 durable entity와 lifecycle service로 관리합니다.

Unity와 Voice 상태 조회·자동 안내는 동일한 12단계 PROCESS projection을 사용합니다.

```text
작업 명령 전달 → 수입검사 → 베이스 설치 → 외벽 팔레트 운반
→ 외벽 설치 → 외벽 빈 팔레트 회수 / 내벽 팔레트 운반
→ 내벽 설치 → 내벽 빈 팔레트 회수 → 조립 결과 검사
→ 지붕 설치 → 완성 주택 운반 → 작업 완료
```

## 4. 외부 인터페이스

| 대상 | Interface | 책임 |
| --- | --- | --- |
| Unity | HTTP / WebSocket | production snapshot, realtime status, inspection·telemetry projection |
| Robot Cell | ROS 2 Action | 조립 task dispatch 및 결과 correlation |
| TurtleBot | ROS 2 Action | 팔레트 transport, empty return, return home |
| Vision | UDP / HTTP | 수입검사 및 per-view 조립 결과 품질검사 request·ACK·result |

실제 장비 동작은 환경 설정과 FMS 실행 상태에 따라 달라집니다. 테스트는 fake transport·fixture를 사용하며, 운영 환경 설정은 저장소에 포함하지 않습니다.

## 5. 디렉터리 구조

```text
server_fms_db/
├── api_server/           # FastAPI routers, API services, static production monitor
├── fms_server/           # FMS worker, robot/transport/vision adapters
├── shared/               # models, schemas, services, realtime contracts
├── telemetry_gateway/    # ROS 2 telemetry → Redis gateway
├── voice_runtime/        # Mic/Wake Word/VAD/TTS runtime and production announcements
├── alembic/              # migration environment and revisions
├── scripts/              # guarded setup, seed, rehearsal and verification scripts
├── tests/                # regression tests
├── .env.example          # safe environment-variable template
├── alembic.ini
├── pyproject.toml
└── requirements.txt
```

## 6. 환경 변수

실제 `.env`는 커밋하지 않습니다. `.env.example`을 복사해 개인 환경에서만 값을 설정합니다.

대표 범주:

- Database / Redis: `DATABASE_URL`, `POSTGRES_TEST_DATABASE_URL`, `REDIS_URL`
- API / Telemetry: `API_HOST`, `API_PORT`, `TELEMETRY_HOST`, `TELEMETRY_PORT`
- Robot / ROS: `ROS_DOMAIN_ID`, Robot Cell·TurtleBot Action name
- Vision: 수입검사 및 조립 결과 품질검사 endpoint / UDP 설정
- Voice: Ollama, Whisper, TTS provider 설정

## 7. 실행 방법

Python 가상환경과 의존성 설치 후, backend 디렉터리에서 실행합니다.

```bash
cd server_fms_db
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

개별 구성 요소의 엔트리포인트는 다음과 같습니다.

```bash
python -m uvicorn api_server.main:app
python -m fms_server.main
python -m uvicorn telemetry_gateway.main:app
python -m voice_runtime.main
```

`./scripts/factory_stack.sh`는 API, FMS, telemetry, Voice Runtime을 프로파일별로 시작하기 위한 launcher입니다. 실제 장비·DB에 연결하기 전에는 설정과 대상 DB를 별도로 확인해야 합니다.

## 8. 테스트 방법

```bash
cd server_fms_db
python -m pytest -qq
```

`postgres_integration`, `redis_integration` marker 테스트는 별도 명시 환경에서만 실행합니다. 실제 Robot Cell, TurtleBot, Vision 장비나 운영 DB를 대상으로 한 명령은 자동 테스트에서 실행하지 않습니다.

## 9. 보안 및 Git 제외 항목

- 실제 `.env`, DB/Redis credential, API key, private certificate는 포함하지 않습니다.
- Python cache·가상환경, ROS build/install/log, runtime log, DB dump, model weight, 녹음·합성 음성, benchmark 결과는 포함하지 않습니다.
- `.env.example`은 공개 가능한 템플릿이며 URL 비밀번호는 `CHANGE_ME` placeholder로만 제공합니다.
- Alembic migration은 source에 포함되지만, migration 실행은 환경·DB 대상 확인 후 별도 승인 절차로 수행해야 합니다.
