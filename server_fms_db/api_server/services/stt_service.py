"""Lazy faster-whisper speech-to-text service."""
from __future__ import annotations
import asyncio, os, tempfile, wave
from pathlib import Path
from fastapi import UploadFile
from shared.config import Settings, get_settings
from shared.schemas.ai import TranscriptionResponse
from api_server.services.voice_timing import voice_timing_stage

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
# Short closed-vocabulary hint for the factory Voice domain. This changes only
# decoding guidance; raw Whisper output remains the API transcript.
VOICE_DOMAIN_INITIAL_PROMPT = (
    "A형 주택, B형 주택, A타입, B타입, 한 채, 두 채, 평지붕, 경사지붕, "
    "자재 재고, 재고 확인해줘, 보여줘, 네, 아니요, 취소, 일시정지, 재개, "
    "평지붕으로, 경사지붕으로, 생산 재개"
)
class AudioValidationError(ValueError): pass
class AudioTooLargeError(ValueError): pass
class STTModelLoadError(RuntimeError): pass
class STTInferenceError(RuntimeError): pass


def _short_wav_duration_seconds(path: str) -> float | None:
    try:
        with wave.open(path, "rb") as wav:
            if wav.getframerate() <= 0:
                return None
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, OSError):
        return None


class STTService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings(); self._model = None; self._load_lock = asyncio.Lock()
    @property
    def loaded(self) -> bool: return self._model is not None
    async def _get_model(self):
        if self._model is not None: return self._model
        async with self._load_lock:
            if self._model is not None: return self._model
            try:
                from faster_whisper import WhisperModel
                self._model = await asyncio.to_thread(WhisperModel, self.settings.whisper_model_size, device=self.settings.whisper_device, compute_type=self.settings.whisper_compute_type)
            except Exception as exc:
                raise STTModelLoadError("Whisper 모델을 불러올 수 없습니다. CUDA 설정을 확인하거나 WHISPER_DEVICE=cpu, WHISPER_COMPUTE_TYPE=int8로 설정하세요.") from exc
        return self._model
    async def transcribe_upload(self, upload: UploadFile) -> TranscriptionResponse:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS and not (upload.content_type or "").startswith("audio/"):
            raise AudioValidationError("WAV, MP3, M4A, FLAC, OGG 음성 파일만 업로드할 수 있습니다.")
        suffix = suffix if suffix in ALLOWED_EXTENSIONS else ".audio"
        limit, total = self.settings.max_audio_file_mb * 1024 * 1024, 0
        handle = tempfile.NamedTemporaryFile(prefix="simfirst-stt-", suffix=suffix, delete=False); path = handle.name
        try:
            with voice_timing_stage("audio_upload_io_ms"):
                with handle:
                    while chunk := await upload.read(1024 * 1024):
                        total += len(chunk)
                        if total > limit: raise AudioTooLargeError(f"음성 파일은 {self.settings.max_audio_file_mb}MB 이하여야 합니다.")
                        handle.write(chunk)
            if not total: raise AudioValidationError("비어 있는 음성 파일입니다.")
            with voice_timing_stage("stt_model_get_ms"):
                model = await self._get_model()
            try:
                prompt_text = VOICE_DOMAIN_INITIAL_PROMPT
                with voice_timing_stage("stt_inference_ms"):
                    segments, info = await asyncio.to_thread(lambda: model.transcribe(path, language=self.settings.whisper_language or None, beam_size=self.settings.whisper_beam_size, initial_prompt=prompt_text))
                    segment_list = await asyncio.to_thread(list, segments)
            except Exception as exc: raise STTInferenceError("음성 파일을 해석하지 못했습니다.") from exc
            text = " ".join(s.text.strip() for s in segment_list if s.text.strip()).strip()
            # A short but non-empty follow-up (model/roof/confirmation) can be
            # suppressed by first-pass no-speech decoding. Retry once only for
            # a real short WAV; successful ordinary transcripts are untouched.
            duration = _short_wav_duration_seconds(path)
            if not text and duration is not None and 0.15 <= duration <= 4.0:
                try:
                    with voice_timing_stage("stt_short_retry_ms"):
                        retry_segments, retry_info = await asyncio.to_thread(
                            lambda: model.transcribe(
                                path, language=self.settings.whisper_language or None,
                                beam_size=self.settings.whisper_beam_size,
                                initial_prompt=prompt_text, temperature=0.0,
                                no_speech_threshold=0.3,
                                condition_on_previous_text=False,
                            )
                        )
                        retry_list = await asyncio.to_thread(list, retry_segments)
                    retry_text = " ".join(s.text.strip() for s in retry_list if s.text.strip()).strip()
                    if retry_text:
                        text, segment_list, info = retry_text, retry_list, retry_info
                except Exception as exc:
                    raise STTInferenceError("음성 파일을 해석하지 못했습니다.") from exc
            if not text: raise AudioValidationError("음성이 너무 짧거나 변환된 텍스트가 없습니다.")
            return TranscriptionResponse(text=text, language=getattr(info, "language", None), language_probability=getattr(info, "language_probability", None), duration_seconds=max((s.end for s in segment_list), default=None), processing_time_ms=0)
        finally:
            if os.path.exists(path): os.unlink(path)

_stt_service: STTService | None = None
def get_stt_service() -> STTService:
    global _stt_service
    if _stt_service is None: _stt_service = STTService()
    return _stt_service
