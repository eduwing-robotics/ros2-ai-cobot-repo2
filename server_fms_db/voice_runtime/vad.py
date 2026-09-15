import numpy as np

class EnergyVAD:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        threshold: float = 0.015,
        min_speech_ms: int = 200,
        trailing_silence_ms: int = 1200
    ):
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.threshold = threshold
        self.min_speech_frames = min_speech_ms // frame_ms
        self.trailing_silence_frames = trailing_silence_ms // frame_ms

        self.reset()

    def reset(self) -> None:
        self.speech_frames = 0
        self.silence_frames = 0
        self.is_speech_started = False

    def process_frame(self, frame_float32: np.ndarray) -> bool:
        """Returns True if utterance completion (trailing silence after speech) is detected."""
        if len(frame_float32) == 0:
            return False

        rms = np.sqrt(np.mean(frame_float32**2))
        is_active = rms > self.threshold

        if is_active:
            self.speech_frames += 1
            self.silence_frames = 0
            if self.speech_frames >= self.min_speech_frames:
                self.is_speech_started = True
        else:
            if self.is_speech_started:
                self.silence_frames += 1
                if self.silence_frames >= self.trailing_silence_frames:
                    return True
        return False
