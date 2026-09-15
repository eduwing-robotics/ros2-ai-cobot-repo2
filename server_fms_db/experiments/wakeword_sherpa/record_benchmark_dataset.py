import argparse
import os
import wave
import numpy as np
import sounddevice as sd

CATEGORIES = {
    "positive_natural": {
        "description": "자연스럽게 이어서 빠르게 '헤이링크' 라고 말하세요.",
        "default_count": 20
    },
    "positive_paused": {
        "description": "단어를 끊어서 또렷하게 '헤이 ... 링크' 라고 말하세요.",
        "default_count": 10
    },
    "negative": {
        "description": "웨이크워드가 아닌 일상어, '헤이', '링크 열어줘' 등을 섞어서 말하세요.",
        "default_count": 20
    },
    "negative_silence": {
        "description": "말을 하지 않고 주변 소음만 녹음하세요.",
        "default_count": 5
    }
}

def record_audio(filename: str, duration: float, sample_rate: int = 16000, device: int = None):
    print("  -> Recording... ", end="", flush=True)
    recording = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='int16', device=device)
    sd.wait()
    print("Done.")

    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(recording.tobytes())

def main():
    parser = argparse.ArgumentParser(description="Record audio dataset for KWS benchmark")
    parser.add_argument("--out-dir", type=str, default="wakeword_benchmark_dataset", help="Output directory")
    parser.add_argument("--duration", type=float, default=2.5, help="Duration of each recording in seconds")
    parser.add_argument("--device", type=int, default=None, help="Microphone device index")
    args = parser.parse_args()

    print("========================================")
    print(" Wakeword Benchmark Dataset Recorder")
    print("========================================")
    print(f"Sample Rate: 16000Hz (Mono)")
    print(f"Duration: {args.duration}s per clip")
    print(f"Output: {args.out_dir}/")
    print("========================================\n")

    for cat_name, cat_info in CATEGORIES.items():
        count = cat_info["default_count"]
        print(f"\n[{cat_name} - {count} clips]")
        print(f"안내: {cat_info['description']}")

        cat_dir = os.path.join(args.out_dir, cat_name)

        for i in range(1, count + 1):
            input(f"\n  [{cat_name} {i}/{count}] Press Enter to start recording...")
            filename = os.path.join(cat_dir, f"{cat_name}_{i:03d}.wav")
            record_audio(filename, args.duration, device=args.device)
            print(f"  Saved: {filename}")

    print("\nDataset recording completed successfully!")

if __name__ == "__main__":
    main()
