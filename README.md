# ros2-ai-cobot-repo2

AI 기반 조립식 주택 자동화 공장 프로젝트 저장소입니다. Vision 검사, 관제, 자재 운반, 음성 인터페이스, 로봇 제어를 영역별 디렉터리로 나누어 관리합니다.

## 저장소 구성

| 디렉터리 | 영역 | 설명 |
|:---|:---|:---|
| [ai_perception](ai_perception/README.md) | AI Perception / Vision | Global Vision · Depth Vision 기반 입고 검사와 조립 품질 검사 |
| [controltower_gui](controltower_gui/README.md) | Control Tower GUI | 공정 · 로봇 · 검사 결과 통합 관제 화면과 수동 제어 |
| [forklift_turtlebot3](forklift_turtlebot3/README.md) | Logistics | TurtleBot3 자재 운반 주행과 포크 리프트 제어 |
| [llm_stt_tts](llm_stt_tts/README.md) | Voice Interface | STT · LLM 의도 해석 · TTS 음성 안내 |
| [robot_control_fr5_zekeep](robot_control_fr5_zekeep/README.md) | Robot Operation | FR5 협동로봇 조립 동작과 ZeKeep 갠트리 제어 |

각 디렉터리는 `README.md` 와 `docs/` 를 두고, 담당자가 해당 영역의 구현과 문서를 관리합니다.

## 작업 규칙

- 작업은 브랜치에서 진행하고 Pull Request 로 `main` 에 병합합니다.
- 커밋 메시지는 `<type>(<scope>): <설명>` 형식을 사용합니다. 예) `feat(ai-perception): add global camera node`
- 모델 가중치, 데이터셋, 런타임 DB, 백업 파일은 커밋하지 않습니다. 자세한 기준은 루트 `.gitignore` 를 참고합니다.
- 티칭 자세, 캘리브레이션 결과, 맵은 재현에 필요하므로 커밋합니다.
