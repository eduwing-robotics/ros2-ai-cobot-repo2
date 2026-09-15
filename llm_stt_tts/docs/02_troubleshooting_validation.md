# Voice AI 문제 해결 및 검증

이 문서는 [상위 README](../README.md)의 문제 해결 요약보다 상세한 기술 기록입니다. 성능 수치는 재현 가능한 결과 artifact가 저장소에 있을 때만 공식 결과로 다룹니다.

## 1. Wake Word 자연 발화

**문제**
영어 기반 KWS에 사용자 Wake Word인 “헤이 링크(HEY LINK)”를 적용할 때, 한국어 자연 발화와 token 표현의 차이로 검출 안정성을 확인할 필요가 있었습니다.

**원인 분석**
실험 폴더에는 baseline keyword `▁HE Y ▁LI N K`와 별도의 surrogate token 후보가 함께 있습니다. `keywords_score`와 `keywords_threshold` 조정뿐 아니라 baseline과 다른 surrogate token sequence를 함께 실험하도록 검토 범위를 확장했습니다. 다만 공개 저장소에는 개별 발화의 raw decode trace가 없으므로 특정 발화가 특정 token으로 decode되었다고 단정하지 않습니다.

**시도**
[`experiments/wakeword_sherpa`](../../server_fms_db/experiments/wakeword_sherpa/)에는 `keywords_score`와 `keywords_threshold` sweep harness, baseline·surrogate keyword 파일, 두 detector 결과를 OR로 결합하는 Dual KWS experiment가 있습니다.

**해결**
현재 production Voice Runtime은 [`keywords.txt`](../../server_fms_db/experiments/wakeword_sherpa/keywords.txt)의 단일 detector를 사용합니다. 이 파일에는 `▁HA T ING ▁CO U P`, `▁PA IN ING`, `▁HA IT ING`, `▁HE Y T T ING` surrogate sequence가 등록되어 있습니다. Dual KWS는 실험 harness로 존재하지만 production Runtime의 구성 요소로 사용되지는 않습니다.

**검증 / 현재 상태**
실험과 validation harness는 공개되어 있으나 결과 CSV/log artifact는 포함되어 있지 않습니다. 따라서 과거 개발 수치를 본 문서의 확정 성능으로 기재하지 않습니다.

## 2. STT 도메인 용어 인식

**문제**
`HOUSE_A`/`HOUSE_B`, 지붕 옵션, 재고·생산 표현처럼 범용 음성 인식 모델에 익숙하지 않을 수 있는 도메인 용어가 있습니다.

**원인 분석**
일반적인 음성 문맥만으로는 조립식 주택 생산 도메인의 고유 명칭을 우선적으로 해석하기 어렵습니다.

**시도**
STT service는 faster-whisper 전사 시 프로젝트 도메인 문구를 포함한 initial prompt를 전달하고, 짧은 WAV에서 빈 결과가 난 경우에만 제한적인 재시도를 수행합니다.

**해결**
도메인 prompt와 짧은 입력 retry를 API Server의 [`STTService`](../../server_fms_db/api_server/services/stt_service.py)에 두어, Voice Runtime이 별도 STT 정책을 갖지 않도록 했습니다.

**검증 / 현재 상태**
STT service와 Voice conversation API를 검증하는 test가 있으며, 공개 결과 artifact가 없으므로 정확도 수치를 확정하지 않습니다.

## 3. LLM 의존 명령 처리 지연

**문제**
생산 상태 조회·재고 조회처럼 명확한 명령도 모두 LLM에 전달하면 불필요한 지연과 결과 변동이 생길 수 있습니다.

**원인 분석**
명확한 도메인 명령과 자유 자연어를 동일한 해석 경로에 놓으면 LLM 호출이 필수 경로가 됩니다.

**시도**
[`CommandInterpreter`](../../server_fms_db/api_server/services/command_interpreter.py)는 상태 조회, 재고 조회, 완전한 생산 요청 형태의 deterministic rule을 먼저 평가하고, 매칭 실패 시에만 Ollama JSON fallback을 사용합니다.

**해결**
Fast-path와 fallback을 결합해 자주 쓰는 명령은 structured intent로 바로 분류하고, 표현이 모호한 입력의 자연어 범위는 fallback에 남겼습니다.

**검증 / 현재 상태**
command interpreter·자연어 benchmark harness·conversation regression test가 저장소에 있습니다. 재현 결과 artifact가 없으므로 latency 또는 정확도 숫자를 본 문서에 쓰지 않습니다.

## 4. Voice·Unity·TTS의 공정 상태 일관성

**문제**
Voice 상태 조회, Unity 관제, 자동 TTS가 각각 steps·delivery·inspection을 따로 해석하면 같은 Job을 서로 다른 단계로 표현할 가능성이 있습니다.

**원인 분석**
수입검사, 자재 운반, 빈 팔레트 반환, PRE_ROOF, outbound는 하나의 실행 Step만으로 현재 공정을 설명하기 어렵습니다.

**시도**
서버의 [`UnityCurrentStageProjectionService`](../../server_fms_db/shared/services/unity_current_stage_projection_service.py)가 durable state에서 canonical 12단계 PROCESS projection을 계산하도록 했습니다.

**해결**
`QUERY_JOB_STATUS`, `/ws/unity` projection, `ProductionAnnouncementDetector`가 `process_stage_code`와 `process_stage_display_name`을 공통 authority로 사용합니다.

**검증 / 현재 상태**
Voice 상태 조회·realtime projection·production announcement 관련 test가 있어 공통 projection 사용을 회귀 검증합니다. 실제 장비 운전 결과를 이 문서에서 보장하지는 않습니다.

## 5. 자동 TTS 중복 안내

**문제**
한 공정 안에서도 production·transport·inspection Redis event가 여러 번 발생할 수 있어, 이벤트 자체를 음성 안내 기준으로 삼으면 같은 안내가 반복될 수 있습니다.

**원인 분석**
Redis event는 상태 변화를 알리는 신호일 뿐, 곧바로 새로운 PROCESS stage 진입을 의미하지 않습니다.

**시도**
`ProductionEventAnnouncementSubscriber`는 event를 받으면 API monitoring route에서 최신 snapshot을 다시 읽습니다. `ProductionAnnouncementDetector`는 snapshot의 PROCESS stage만 확인합니다.

**해결**
안내 key를 `(job_id, "PROCESS_STAGE", stage_code)`로 dedupe하고, Runtime 시작 시 기존 Job의 현재 stage는 baseline만 기록해 과거 안내를 재생하지 않습니다. `QA_FAILED`, `PRE_ROOF_FAILED`, Robot Cell fault, pause/resume은 공정 stage와 별도의 중요한 알림으로 유지합니다.

**검증 / 현재 상태**
동일 stage의 세부 이벤트 변화와 Runtime restart baseline을 다루는 production event TTS test가 있습니다. TTS audio playback과 실제 장비 I/O는 test fixture에서 수행하지 않습니다.

## 검증 전략과 범위

| 검증 영역 | 저장소에서 확인 가능한 방법 |
| --- | --- |
| 명령 해석 | unit test, 자연어 command benchmark harness |
| 생산 대화 | service/API regression test, pending request 상태 전이 검증 |
| STT·TTS | service/API 및 Voice Runtime test, fake·mock 기반 검증 |
| 자동 공정 안내 | PROCESS projection snapshot과 Redis-event-triggered readback test |
| Wake Word | keyword·threshold·Dual KWS experiment harness 및 validation script |

이 검증 구조는 실제 Robot, TurtleBot, Vision, microphone, speaker를 실행했다는 뜻이 아닙니다. 외부 장비·실제 오디오 환경의 E2E 검증은 별도 운전 환경에서 수행해야 합니다.
