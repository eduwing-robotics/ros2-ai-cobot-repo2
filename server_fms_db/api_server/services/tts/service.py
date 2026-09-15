from __future__ import annotations
import asyncio, time
from dataclasses import dataclass
from shared.config import Settings, get_settings
from .base import TTSProvider
from .edge_tts_provider import EdgeTTSProvider
class TTSConfigurationError(RuntimeError): pass
class TTSConnectionError(RuntimeError): pass
class TTSTimeoutError(RuntimeError): pass
class TTSSynthesisError(RuntimeError): pass
@dataclass
class TTSResult:
    audio: bytes; content_type: str; file_extension: str; provider: str; voice: str | None; processing_time_ms: float
class TTSService:
    def __init__(self, settings: Settings|None=None, provider: TTSProvider|None=None): self.settings=settings or get_settings(); self._provider=provider
    def _get_provider(self):
        if self._provider: return self._provider
        if self.settings.tts_provider != "edge": raise TTSConfigurationError("지원하지 않는 TTS provider입니다.")
        try: self._provider=EdgeTTSProvider(self.settings.tts_voice, self.settings.tts_rate, self.settings.tts_volume, self.settings.tts_pitch)
        except ValueError as e: raise TTSConfigurationError(str(e)) from e
        return self._provider
    async def synthesize(self, text: str) -> TTSResult:
        text=text.strip()
        if not text: raise ValueError("TTS 텍스트는 비어 있을 수 없습니다.")
        if len(text)>self.settings.tts_max_text_length: raise ValueError(f"TTS 텍스트는 {self.settings.tts_max_text_length}자 이하여야 합니다.")
        provider=self._get_provider(); started=time.perf_counter()
        try: audio=await asyncio.wait_for(provider.synthesize(text), timeout=self.settings.tts_timeout_seconds)
        except asyncio.TimeoutError as e: raise TTSTimeoutError("TTS 응답 시간이 초과되었습니다.") from e
        except Exception as e: raise TTSConnectionError("TTS provider에 연결하거나 음성을 생성하지 못했습니다.") from e
        if not audio: raise TTSSynthesisError("TTS provider가 빈 오디오를 반환했습니다.")
        return TTSResult(audio,provider.content_type,provider.file_extension,self.settings.tts_provider,self.settings.tts_voice or None,(time.perf_counter()-started)*1000)
    async def health(self) -> bool:
        try: return await self._get_provider().health()
        except TTSConfigurationError: return False
_service=None
def get_tts_service():
 global _service
 if _service is None: _service=TTSService()
 return _service
