from __future__ import annotations
import edge_tts
from .base import TTSProvider
class EdgeTTSProvider(TTSProvider):
    def __init__(self, voice: str, rate: str, volume: str, pitch: str) -> None:
        if not voice: raise ValueError("TTS_VOICE 설정이 필요합니다.")
        self.voice, self.rate, self.volume, self.pitch = voice, rate, volume, pitch
    async def synthesize(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume, pitch=self.pitch)
        chunks=[]
        async for item in communicate.stream():
            if item["type"] == "audio": chunks.append(item["data"])
        return b"".join(chunks)
    async def health(self) -> bool:
        return bool(self.voice)
