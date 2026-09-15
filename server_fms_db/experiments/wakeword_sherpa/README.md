# Wake Word Detection PoC with sherpa-onnx

이 디렉터리는 `sherpa-onnx`를 사용한 독립적인 웨이크워드(Wake word) 감지 기술 검증용 PoC(Proof of Concept)입니다. 목표 웨이크워드는 "헤이 링크"(HEY LINK)입니다.

## 1. 개요
* **엔진**: sherpa-onnx (Keyword Spotting 기능)
* **웨이크워드**: HEY LINK (한국식 발음 "헤이 링크"로 테스트 예정)
* **모델**: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (영어 모델)
* **목표**: 마이크 실시간 감지 안정성 테스트 및 파라미터 튜닝

## 2. 사전 준비 및 의존성 설치
**주의**: 실시간 오디오 캡처를 위해 OS 수준의 패키지가 필요할 수 있습니다.
```bash
# Ubuntu에서 sounddevice가 ALSA/PortAudio를 사용할 수 있도록 설치
sudo apt-get update
sudo apt-get install -y libportaudio2
```

Python 가상환경 및 패키지 설치:
```bash
# 기존 프로젝트 .venv 오염 방지를 위해 별도 가상환경 생성
python3.12 -m venv .venv
source .venv/bin/activate

# 의존성 설치 (기존 서버 패키지와 무관)
pip install sounddevice numpy sherpa-onnx
```

## 3. 실행 방법
```bash
cd experiments/wakeword_sherpa
.venv/bin/python wakeword_test.py --score 1.5 --threshold 0.25
```

## 4. 튜닝 파라미터
- `--score`: Keyword boosting score. 값이 클수록 감지가 쉬워지나 False Positive(오탐)가 증가합니다. (기본값: 1.5)
- `--threshold`: Trigger threshold (0.0 ~ 1.0). 값이 클수록 감지가 엄격해져 False Positive는 줄지만 실제 발음을 놓칠 수 있습니다. (기본값: 0.25)
- `--device`: 사용할 마이크 번호 (입력하지 않으면 OS 기본 마이크 사용)

## 5. 수동 테스트 계획 (사용자 테스트 필요)
이 프로그램을 실행한 뒤, 다음 5가지 테스트를 직접 수행해 주시기 바랍니다.

### Test A - 정상 발음
* **방법**: "헤이 링크"를 20회 말합니다.
* **기록**: 성공 횟수 / 실패 횟수

### Test B - 거리 테스트
* **방법**: 마이크와의 거리를 달리하여 말해봅니다.
    1. 가까운 거리 (30cm 이내)
    2. 약 1m 거리
    3. 실제 현장 데모 예정 거리 (예: 2m~3m)
* **기록**: 각 거리별 감지율 및 체감 성능

### Test C - 화자 테스트
* **방법**: 여러 명의 팀원이 각자 "헤이 링크"를 말합니다.
* **기록**: 화자(성별, 목소리 톤)에 따른 감지 편차 확인

### Test D - False Positive(오탐) 테스트
* **방법**: 웨이크워드가 아닌 일반 대화를 진행하거나 유사 발음을 말합니다.
    * "링크 보내줘"
    * "헤이 거기"
    * "생산 시작해줘"
    * "HOUSE A"
    * "현재 재고 알려줘"
* **기록**: 잘못 감지(WAKE)된 횟수와 어떤 문장에서 오탐이 발생했는지 기록

### Test E - 소음 테스트
* **방법**: 실제 환경과 유사한 소음(사람 대화 소리, 로봇/팬 소리 등)을 틀어놓고 테스트합니다.
* **기록**: 소음 환경에서의 감지율 하락폭 및 오탐률 변화

## 6. 종료
프로그램은 `Ctrl+C`를 누르면 종료되며, 종료 시 총 감지 횟수와 실행 시간이 표시됩니다.
