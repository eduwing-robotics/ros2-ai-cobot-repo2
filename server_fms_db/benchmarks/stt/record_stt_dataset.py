"""
Recorder for STT Benchmark Dataset.
"""

import argparse
import csv
import os
import wave

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    print("Please install sounddevice (pip install sounddevice)")
    import sys
    sys.exit(1)

SENTENCES = [
    # Short commands
    {"id": "001", "category": "short_command", "text": "현재 작업 상태 알려줘"},
    {"id": "002", "category": "short_command", "text": "생산을 시작해줘"},
    {"id": "003", "category": "short_command", "text": "작업을 일시정지해줘"},
    {"id": "004", "category": "short_command", "text": "생산을 재개해줘"},
    {"id": "005", "category": "short_command", "text": "현재 공정 알려줘"},
    # Domain specific
    {"id": "006", "category": "domain", "text": "에이 타입 주택 생산을 시작해줘"},
    {"id": "007", "category": "domain", "text": "비 타입 주택으로 변경해줘"},
    {"id": "008", "category": "domain", "text": "현재 세 번째 공정 진행 상황 알려줘"},
    {"id": "009", "category": "domain", "text": "내부벽 설치 상태 확인해줘"},
    {"id": "010", "category": "domain", "text": "외벽 설치가 끝났는지 확인해줘"},
    {"id": "011", "category": "domain", "text": "구조물 품질 검사 결과 알려줘"},
    {"id": "012", "category": "domain", "text": "로봇 셀 상태 확인해줘"},
    # Long sentences
    {"id": "013", "category": "long_sentence", "text": "현재 생산 중인 주택이 어느 단계까지 진행됐는지 알려줘"},
    {"id": "014", "category": "long_sentence", "text": "작업이 실패했다면 어느 공정에서 문제가 발생했는지 알려줘"},
    {"id": "015", "category": "long_sentence", "text": "지금 진행 중인 생산 작업을 안전하게 일시정지해줘"},
    # Alphanumeric
    {"id": "016", "category": "alphanumeric", "text": "S3 공정 상태 알려줘"},
    {"id": "017", "category": "alphanumeric", "text": "FR5 상태 확인해줘"},
    {"id": "018", "category": "alphanumeric", "text": "HOUSE A 생산 상태 알려줘"},
]

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
    parser = argparse.ArgumentParser(description="Record audio dataset for STT benchmark")
    parser.add_argument("--out-dir", type=str, default="benchmarks/stt/dataset", help="Output directory")
    parser.add_argument("--device", type=int, default=None, help="Microphone device index")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    manifest_path = os.path.join(args.out_dir, "dataset_manifest.csv")

    print("========================================")
    print(" STT Benchmark Dataset Recorder")
    print("========================================")
    print("실제 공장에서 지시하듯 자연스러운 억양과 속도로 말해주세요.")
    print(f"Output: {args.out_dir}/")
    print("========================================\n")

    manifest = []

    for i, item in enumerate(SENTENCES):
        # Determine duration based on text length
        duration = 3.0 if len(item["text"]) < 15 else 5.0

        filename = f"{item['id']}.wav"
        filepath = os.path.join(args.out_dir, filename)

        print(f"\n[{i+1} / {len(SENTENCES)}] Category: {item['category']}")
        print(f"다음 문장을 자연스럽게 읽어주세요:\n\n    \"{item['text']}\"\n")
        input("    Press Enter to start recording...")

        record_audio(filepath, duration, device=args.device)
        print(f"  Saved: {filepath}")

        manifest.append({
            "id": item["id"],
            "category": item["category"],
            "filename": filename,
            "reference_text": item["text"]
        })

    # Save manifest
    with open(manifest_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["id", "category", "filename", "reference_text"])
        writer.writeheader()
        writer.writerows(manifest)

    print(f"\nDataset recording completed successfully! Manifest saved at {manifest_path}")

if __name__ == "__main__":
    main()
