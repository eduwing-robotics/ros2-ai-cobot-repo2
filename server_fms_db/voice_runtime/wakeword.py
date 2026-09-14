import os
import numpy as np

class WakewordDetector:
    def __init__(self, config_dir: str = "experiments/wakeword_sherpa/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"):
        try:
            import sherpa_onnx
            self.kws = sherpa_onnx.KeywordSpotter(
                tokens=os.path.join(config_dir, "tokens.txt"),
                encoder=os.path.join(config_dir, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
                decoder=os.path.join(config_dir, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
                joiner=os.path.join(config_dir, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
                keywords_file="experiments/wakeword_sherpa/keywords.txt",
                num_threads=1,
                provider="cpu",
                keywords_score=1.5,
                keywords_threshold=0.25,
            )
            self.stream = self.kws.create_stream()
            self.available = True
        except ImportError:
            self.available = False
            print("[WakewordDetector] sherpa-onnx not found. Wakeword detection is disabled.")

    def process_audio(self, samples: np.ndarray, sample_rate: int = 16000) -> bool:
        if not self.available:
            return False

        samples = samples.reshape(-1)
        self.stream.accept_waveform(sample_rate, samples)

        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            result = self.kws.get_result(self.stream)
            if result:
                self.kws.reset_stream(self.stream)
                return True
        return False
