import io
import queue
import time
import wave
import os
import subprocess
import tempfile

import numpy as np
import sounddevice as sd


class AudioPlaybackError(RuntimeError):
    """Raised when the local speaker player cannot complete playback."""


class _TTSPlaybackHandle:
    def __init__(self, process, temp_name: str) -> None:
        self._process = process
        self._temp_name = temp_name
        self._closed = False
    def wait(self) -> None:
        try:
            result = self._process.wait()
            if result != 0:
                raise AudioPlaybackError(f"ffplay exited with status {result}.")
        finally:
            self._cleanup()
    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
        self._cleanup()
    def _cleanup(self) -> None:
        if not self._closed:
            self._closed = True
            if os.path.exists(self._temp_name):
                os.remove(self._temp_name)


class AudioIO:
    def __init__(self, device: int | None = None, sample_rate: int = 16000):
        self.device = device
        self.sample_rate = sample_rate
        self.stream = None
        self.queue = queue.Queue()

    @property
    def is_recording(self) -> bool:
        """Whether an actual microphone InputStream is currently owned."""
        return self.stream is not None

    def start_recording(self):
        # The wake-word listener may already own this stream. Reusing it avoids
        # opening a second microphone device on wake detection; after a user
        # turn ``stop_recording`` clears it and this method starts it again.
        if self.stream is not None:
            return
        while not self.queue.empty():
            self.queue.get_nowait()

        self.stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.sample_rate,
            dtype='float32',
            callback=self._audio_callback,
        )
        self.stream.start()

    def stop_recording(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _audio_callback(self, indata, frames, time, status):
        self.queue.put(indata.copy())

    def play_chime(self):
        fs = 16000
        t = np.linspace(0, 0.15, int(fs * 0.15), endpoint=False)
        note = 0.5 * np.sin(2 * np.pi * 880 * t)
        note *= np.linspace(1, 0, len(t))
        sd.play(note, fs)
        sd.wait()

    def start_tts(self, audio_bytes: bytes) -> _TTSPlaybackHandle:
        """Start local ffplay without blocking the voice event loop."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as file:
            file.write(audio_bytes)
            temp_name = file.name
        try:
            process = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", temp_name])
        except FileNotFoundError as exc:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise AudioPlaybackError("ffplay is not installed or not available on PATH.") from exc
        return _TTSPlaybackHandle(process, temp_name)

    def play_tts(self, audio_bytes: bytes) -> float:
        """Compatibility blocking wrapper used by older callers."""
        started = time.perf_counter()
        self.start_tts(audio_bytes).wait()
        return (time.perf_counter() - started) * 1000

    @staticmethod
    def float32_to_wav_bytes(float_data: np.ndarray, sample_rate: int = 16000) -> bytes:
        audio_data = np.int16(float_data * 32767)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data.tobytes())
        return buffer.getvalue()
