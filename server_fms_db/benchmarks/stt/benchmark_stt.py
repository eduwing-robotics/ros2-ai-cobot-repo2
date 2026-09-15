"""
STT Benchmark tool.
"""

import argparse
import csv
import datetime
import json
import os
import platform
import subprocess
import sys
import time
import wave
import re
import numpy as np

# Make sure we can import from shared
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))
try:
    from shared.config import get_settings
    from faster_whisper import WhisperModel
except ImportError as e:
    print(f"Failed to import project dependencies: {e}")
    sys.exit(1)


def get_git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    except Exception:
        return "unavailable"


def get_cpu_info() -> str:
    try:
        if platform.system() == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
    except Exception:
        pass
    return platform.processor() or "unavailable"


def get_gpu_info() -> str:
    try:
        if platform.system() == "Linux":
            output = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).decode().strip()
            if output:
                return output.split('\n')[0]
    except Exception:
        pass
    return "unavailable"


def normalize_text(text: str) -> str:
    """
    Normalize text for WER calculation.
    Keep alphanumeric, korean characters and spaces, lowercase everything.
    """
    text = text.lower()
    text = re.sub(r'[^\w\s가-힣]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_for_cer(text: str) -> str:
    """
    Normalize text for CER calculation.
    Remove all spaces so only character operations are counted.
    """
    return normalize_text(text).replace(" ", "")


def levenshtein(s1, s2):
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def calculate_wer(ref: str, hyp: str):
    ref_words = normalize_text(ref).split()
    hyp_words = normalize_text(hyp).split()
    edits = levenshtein(ref_words, hyp_words)
    return edits, len(ref_words)


def calculate_cer(ref: str, hyp: str):
    ref_chars = normalize_for_cer(ref)
    hyp_chars = normalize_for_cer(hyp)
    edits = levenshtein(ref_chars, hyp_chars)
    return edits, len(ref_chars)


def get_audio_duration(filepath: str) -> float:
    with wave.open(filepath, 'rb') as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate)


def main():
    parser = argparse.ArgumentParser(description="STT Benchmark Tool")
    parser.add_argument("--dataset", type=str, default="benchmarks/stt/dataset", help="Dataset directory")
    parser.add_argument("--out-dir", type=str, default="benchmarks/stt/results", help="Output directory")
    parser.add_argument("--label", type=str, default="baseline", help="Benchmark label")
    args = parser.parse_args()

    manifest_path = os.path.join(args.dataset, "dataset_manifest.csv")
    if not os.path.exists(manifest_path):
        print("Benchmark tool implementation complete.")
        print(f"User recording is required before measuring the real baseline.")
        print(f"Manifest not found at {manifest_path}")
        sys.exit(0)

    dataset = []
    with open(manifest_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            filepath = os.path.join(args.dataset, row["filename"])
            if not os.path.exists(filepath):
                print(f"Warning: Audio file missing: {filepath}")
                continue
            row["filepath"] = filepath
            dataset.append(row)

    if not dataset:
        print("Dataset is empty.")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{args.label}_{timestamp}"

    settings = get_settings()

    print("==================================================")
    print("STT Benchmark Initialization")
    print("==================================================")
    print(f"Model       : {settings.whisper_model_size}")
    print(f"Device      : {settings.whisper_device}")
    print(f"Compute Type: {settings.whisper_compute_type}")
    print(f"Language    : {settings.whisper_language}")
    print(f"Beam Size   : {settings.whisper_beam_size}")
    print("--------------------------------------------------")
    print("Loading model...")

    start_load = time.perf_counter()
    model = WhisperModel(
        settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type
    )
    model_load_ms = (time.perf_counter() - start_load) * 1000
    print(f"Model loaded in {model_load_ms:.2f} ms")

    results = []
    total_edits_wer = 0
    total_ref_wer = 0
    total_edits_cer = 0
    total_ref_cer = 0
    exact_matches = 0
    first_transcription_ms = None
    latencies = []
    rtfs = []

    category_stats = {}

    print("\nRunning Benchmark...")
    for idx, item in enumerate(dataset):
        print(f"  [{idx+1}/{len(dataset)}] {item['filename']} ... ", end="", flush=True)

        try:
            duration = get_audio_duration(item["filepath"])

            start_infer = time.perf_counter()
            segments, info = model.transcribe(
                item["filepath"],
                language=settings.whisper_language or None,
                beam_size=settings.whisper_beam_size
            )
            segment_list = list(segments)
            infer_ms = (time.perf_counter() - start_infer) * 1000

            if first_transcription_ms is None:
                first_transcription_ms = infer_ms

            text = " ".join(s.text.strip() for s in segment_list if s.text.strip()).strip()

            ref = item["reference_text"]
            hyp = text

            norm_ref = normalize_text(ref)
            norm_hyp = normalize_text(hyp)

            exact_match = (norm_ref == norm_hyp)
            if exact_match:
                exact_matches += 1

            w_edits, w_len = calculate_wer(ref, hyp)
            c_edits, c_len = calculate_cer(ref, hyp)

            wer_val = w_edits / w_len if w_len > 0 else 0
            cer_val = c_edits / c_len if c_len > 0 else 0

            total_edits_wer += w_edits
            total_ref_wer += w_len
            total_edits_cer += c_edits
            total_ref_cer += c_len

            rtf = (infer_ms / 1000.0) / duration if duration > 0 else 0
            latencies.append(infer_ms)
            rtfs.append(rtf)

            if item["category"] not in category_stats:
                category_stats[item["category"]] = {"total": 0, "exact": 0}
            category_stats[item["category"]]["total"] += 1
            if exact_match:
                category_stats[item["category"]]["exact"] += 1

            results.append({
                "run_id": run_id,
                "id": item["id"],
                "category": item["category"],
                "filename": item["filename"],
                "reference_text": ref,
                "hypothesis_text": hyp,
                "normalized_reference": norm_ref,
                "normalized_hypothesis": norm_hyp,
                "exact_match": str(exact_match).lower(),
                "wer": wer_val,
                "cer": cer_val,
                "audio_duration_sec": duration,
                "latency_ms": infer_ms,
                "rtf": rtf,
                "error": ""
            })
            print(f"OK (Latency: {infer_ms:.1f}ms, RTF: {rtf:.2f})")

        except Exception as e:
            print(f"FAILED: {e}")
            results.append({
                "run_id": run_id,
                "id": item["id"],
                "category": item["category"],
                "filename": item["filename"],
                "reference_text": item["reference_text"],
                "hypothesis_text": "",
                "normalized_reference": "",
                "normalized_hypothesis": "",
                "exact_match": "false",
                "wer": 0,
                "cer": 0,
                "audio_duration_sec": 0,
                "latency_ms": 0,
                "rtf": 0,
                "error": str(e)
            })

    # Calculate Summaries
    agg_wer = total_edits_wer / total_ref_wer if total_ref_wer > 0 else 0
    agg_cer = total_edits_cer / total_ref_cer if total_ref_cer > 0 else 0

    mean_lat = np.mean(latencies) if latencies else 0
    p50_lat = np.percentile(latencies, 50) if latencies else 0
    p95_lat = np.percentile(latencies, 95) if latencies else 0
    min_lat = np.min(latencies) if latencies else 0
    max_lat = np.max(latencies) if latencies else 0
    mean_rtf = np.mean(rtfs) if rtfs else 0

    acc = exact_matches / len(dataset) if dataset else 0

    # Write Metadata
    metadata = {
        "measured_at": datetime.datetime.now().isoformat(),
        "label": args.label,
        "git_commit": get_git_commit(),
        "os": platform.platform(),
        "cpu": get_cpu_info(),
        "gpu": get_gpu_info(),
        "python_version": sys.version.replace('\n', ''),
        "model": settings.whisper_model_size,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "language": settings.whisper_language,
        "beam_size": settings.whisper_beam_size,
        "vad_filter": "False (default)",
        "dataset_path": args.dataset,
        "total_audio_files": len(dataset)
    }

    meta_path = os.path.join(args.out_dir, f"stt_benchmark_metadata_{timestamp}.json")
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=4)

    # Write Trials
    trials_path = os.path.join(args.out_dir, f"stt_benchmark_trials_{timestamp}.csv")
    trials_fields = [
        "run_id", "id", "category", "filename", "reference_text", "hypothesis_text",
        "normalized_reference", "normalized_hypothesis", "exact_match",
        "wer", "cer", "audio_duration_sec", "latency_ms", "rtf", "error"
    ]
    with open(trials_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=trials_fields)
        writer.writeheader()
        writer.writerows(results)

    # Write Summary
    summary_path = os.path.join(args.out_dir, f"stt_benchmark_summary_{timestamp}.csv")
    summary_fields = [
        "run_id", "measured_at", "label", "model", "device", "compute_type",
        "total_sentences", "exact_matches", "sentence_accuracy",
        "mean_wer", "mean_cer",
        "mean_latency_ms", "p50_latency_ms", "p95_latency_ms", "min_latency_ms", "max_latency_ms",
        "mean_rtf", "model_load_ms", "first_transcription_ms"
    ]

    for cat in category_stats.keys():
        summary_fields.append(f"{cat}_accuracy")

    summary_row = {
        "run_id": run_id,
        "measured_at": metadata["measured_at"],
        "label": args.label,
        "model": settings.whisper_model_size,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "total_sentences": len(dataset),
        "exact_matches": exact_matches,
        "sentence_accuracy": acc,
        "mean_wer": agg_wer,
        "mean_cer": agg_cer,
        "mean_latency_ms": mean_lat,
        "p50_latency_ms": p50_lat,
        "p95_latency_ms": p95_lat,
        "min_latency_ms": min_lat,
        "max_latency_ms": max_lat,
        "mean_rtf": mean_rtf,
        "model_load_ms": model_load_ms,
        "first_transcription_ms": first_transcription_ms or 0
    }

    for cat, cstats in category_stats.items():
        summary_row[f"{cat}_accuracy"] = cstats["exact"] / cstats["total"] if cstats["total"] > 0 else 0

    with open(summary_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerow(summary_row)

    print("\n==================================================")
    print("STT Baseline")
    print("==================================================")
    print(f"Model          : {settings.whisper_model_size}")
    print(f"Device         : {settings.whisper_device}")
    print(f"Compute Type   : {settings.whisper_compute_type}")
    print("")
    print(f"Sentences      : {len(dataset)}")
    print(f"Exact Match    : {exact_matches} / {len(dataset)}")
    print(f"Accuracy       : {acc*100:.2f}%")
    print("")
    print(f"WER            : {agg_wer*100:.2f}%")
    print(f"CER            : {agg_cer*100:.2f}%")
    print("")
    print(f"Mean Latency   : {mean_lat:.0f} ms")
    print(f"P50 Latency    : {p50_lat:.0f} ms")
    print(f"P95 Latency    : {p95_lat:.0f} ms")
    print("")
    print(f"Mean RTF       : {mean_rtf:.2f}")
    print("")
    print(f"Model Load     : {model_load_ms/1000.0:.2f} sec")
    print(f"First Request  : {first_transcription_ms:.0f} ms" if first_transcription_ms else "First Request  : N/A")
    print("==================================================")

if __name__ == "__main__":
    main()
