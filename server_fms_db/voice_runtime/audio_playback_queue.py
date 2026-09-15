"""Single in-process speaker queue shared by conversation and production TTS."""
from __future__ import annotations
import asyncio, logging
from .production_announcements import AnnouncementPriority
logger=logging.getLogger(__name__)
class AudioPlaybackQueue:
    def __init__(self, *, api_client, audio_io) -> None:
        self._api=api_client; self._audio=audio_io; self._queue: asyncio.PriorityQueue=asyncio.PriorityQueue(); self._seq=0; self._task=None; self._current=None; self._current_priority=None
    def start(self):
        if self._task is None: self._task=asyncio.create_task(self._run(),name="voice-audio-playback")
    async def stop(self):
        if self._current_priority is AnnouncementPriority.NORMAL: self._interrupt_current()
        if self._task: self._task.cancel();
        if self._task:
            try: await self._task
            except asyncio.CancelledError: pass
        self._task=None
    def enqueue(self,text:str,priority:AnnouncementPriority=AnnouncementPriority.NORMAL):
        self.start()
        loop=asyncio.get_running_loop(); done=loop.create_future(); self._seq+=1
        if priority is AnnouncementPriority.HIGH and self._current_priority is AnnouncementPriority.NORMAL: self._interrupt_current()
        self._queue.put_nowait((-int(priority),self._seq,text,done)); return done
    def _interrupt_current(self):
        handle=self._current
        if handle is not None:
            try: handle.stop()
            except Exception: logger.exception("Could not interrupt normal TTS playback")
    async def _run(self):
        while True:
            priority_code, _, text, done = await self._queue.get(); self._current=None; self._current_priority=AnnouncementPriority.HIGH if priority_code == -int(AnnouncementPriority.HIGH) else AnnouncementPriority.NORMAL
            try:
                audio=await self._api.get_tts_audio(text)
                if hasattr(self._audio,"start_tts"):
                    self._current=self._audio.start_tts(audio)
                    await asyncio.to_thread(self._current.wait)
                else:
                    await asyncio.to_thread(self._audio.play_tts,audio)
                if not done.done(): done.set_result(None)
            except asyncio.CancelledError: raise
            except Exception as exc:
                logger.warning("TTS playback failed without affecting production: %s",exc)
                if not done.done(): done.set_result(None)
            finally:
                self._current=None; self._current_priority=None
