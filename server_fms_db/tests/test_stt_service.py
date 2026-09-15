from __future__ import annotations

import asyncio
import io
import wave

import numpy as np
import pytest

from fastapi import UploadFile

from api_server.services.stt_service import STTService, VOICE_DOMAIN_INITIAL_PROMPT
from shared.config import Settings


class _Segment:
    def __init__(self, text: str, end: float = 0.5) -> None:
        self.text = text
        self.end = end


class _Info:
    language = "ko"
    language_probability = 0.99


class _Model:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, _path: str, **kwargs: object):
        self.calls.append(kwargs)
        return iter((_Segment(" 평지봉 자제제고 안녕 "),)), _Info()


def test_domain_initial_prompt_is_small_and_contains_closed_voice_terms() -> None:
    for term in ("A형 주택", "B형 주택", "두 채", "평지붕", "경사지붕", "평지붕으로", "경사지붕으로", "자재 재고", "아니요", "취소", "일시정지", "재개", "생산 재개"):
        assert term in VOICE_DOMAIN_INITIAL_PROMPT
    assert len(VOICE_DOMAIN_INITIAL_PROMPT) < 160


def test_stt_passes_domain_prompt_and_preserves_raw_whisper_transcript() -> None:
    settings = Settings(whisper_language="ko", whisper_beam_size=5)
    service = STTService(settings)
    model = _Model()
    service._model = model
    upload = UploadFile(filename="voice.wav", file=io.BytesIO(b"not-decoded-by-fake"))

    response = asyncio.run(service.transcribe_upload(upload))

    assert model.calls == [{
        "language": "ko",
        "beam_size": 5,
        "initial_prompt": VOICE_DOMAIN_INITIAL_PROMPT,
    }]
    # No post-ASR lexical correction is hidden in the generic STT service.
    assert response.text == "평지봉 자제제고 안녕"
    assert response.language == "ko"


def _wav(seconds: float) -> io.BytesIO:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as value:
        value.setnchannels(1); value.setsampwidth(2); value.setframerate(16000)
        value.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())
    buffer.seek(0)
    return buffer


class _SequenceModel:
    def __init__(self, texts): self.texts = iter(texts); self.calls = []
    def transcribe(self, _path, **kwargs):
        self.calls.append(kwargs)
        return iter((_Segment(next(self.texts)),)), _Info()


def test_short_empty_first_pass_retries_once_with_supported_decode_options() -> None:
    service = STTService(Settings(whisper_language="ko", whisper_beam_size=5))
    model = _SequenceModel(["", "B형"]); service._model = model
    response = asyncio.run(service.transcribe_upload(UploadFile(filename="short.wav", file=_wav(0.5))))
    assert response.text == "B형" and len(model.calls) == 2
    assert model.calls[1]["temperature"] == 0.0
    assert model.calls[1]["no_speech_threshold"] == 0.3
    assert model.calls[1]["condition_on_previous_text"] is False


def test_successful_first_pass_never_retries() -> None:
    service = STTService(Settings(whisper_language="ko", whisper_beam_size=5))
    model = _SequenceModel(["재고 알려줘"]); service._model = model
    assert asyncio.run(service.transcribe_upload(UploadFile(filename="short.wav", file=_wav(0.5)))).text == "재고 알려줘"
    assert len(model.calls) == 1


def test_short_empty_second_pass_remains_recoverable_validation_error() -> None:
    from api_server.services.stt_service import AudioValidationError
    service = STTService(Settings(whisper_language="ko", whisper_beam_size=5))
    model = _SequenceModel(["", ""]); service._model = model
    with pytest.raises(AudioValidationError):
        asyncio.run(service.transcribe_upload(UploadFile(filename="short.wav", file=_wav(0.5))))
    assert len(model.calls) == 2
