from __future__ import annotations
from abc import ABC, abstractmethod
class TTSProvider(ABC):
    content_type = "audio/mpeg"
    file_extension = "mp3"
    @abstractmethod
    async def synthesize(self, text: str) -> bytes: ...
    @abstractmethod
    async def health(self) -> bool: ...
