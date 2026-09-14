import argparse
import datetime
import sys
import time

import numpy as np
import sherpa_onnx
import sounddevice as sd

def create_kws(args):
    return sherpa_onnx.KeywordSpotter(
        tokens="models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/tokens.txt",
        encoder="models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        decoder="models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        joiner="models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        keywords_file="keywords.txt",
        num_threads=2,
        provider="cpu",
        keywords_score=args.score,
        keywords_threshold=args.threshold,
    )

def main():
    parser = argparse.ArgumentParser(description="Wake word detection PoC with sherpa-onnx")
    parser.add_argument("--score", type=float, default=1.5, help="Boosting score for keywords")
    parser.add_argument("--threshold", type=float, default=0.25, help="Trigger threshold for keywords")
    parser.add_argument("--device", type=int, default=None, help="Microphone device index")
    args = parser.parse_args()

    kws = create_kws(args)
    sample_rate = 16000

    stream = kws.create_stream()

    print("-" * 32)
    print("Wake word detector started")
    print("Keyword: HEY LINK")
    print(f"Parameters: threshold={args.threshold}, score={args.score}")
    print("Listening...")
    print("Press Ctrl+C to stop")
    print("-" * 32)

    detected_count = 0
    start_time = datetime.datetime.now()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Audio callback status: {status}", file=sys.stderr)

        samples = indata.reshape(-1)
        stream.accept_waveform(sample_rate, samples)

    try:
        with sd.InputStream(device=args.device, channels=1, dtype="float32", samplerate=sample_rate, callback=audio_callback):
            while True:
                while kws.is_ready(stream):
                    kws.decode_stream(stream)
                    result = kws.get_result(stream)
                    if result:
                        detected_count += 1
                        now = datetime.datetime.now()
                        print(f"[{now.strftime('%H:%M:%S')}] [WAKE] {result} detected")
                        kws.reset_stream(stream)

                time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    end_time = datetime.datetime.now()
    runtime = end_time - start_time
    minutes, seconds = divmod(runtime.total_seconds(), 60)

    print("\n" + "-" * 32)
    print("Wake word test finished")
    print(f"Detected: {detected_count}")
    print(f"Runtime: {int(minutes)}m {int(seconds)}s")
    print("-" * 32)

if __name__ == "__main__":
    main()
