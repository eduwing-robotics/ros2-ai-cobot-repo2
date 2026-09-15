import argparse
import csv
import glob
import os
import wave
import sys
import numpy as np

import sherpa_onnx

def create_kws(score: float, threshold: float):
    # Base paths as in wakeword_test.py
    base_dir = os.path.dirname(os.path.abspath(__file__))
    model_dir = os.path.join(base_dir, "models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01")

    # Validate model exists so we don't crash mysteriously
    if not os.path.exists(model_dir):
        print(f"Error: Model directory not found at {model_dir}")
        print("Please run this script from the correct working directory or download the model.")
        sys.exit(1)

    return sherpa_onnx.KeywordSpotter(
        tokens=os.path.join(model_dir, "tokens.txt"),
        encoder=os.path.join(model_dir, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
        decoder=os.path.join(model_dir, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
        joiner=os.path.join(model_dir, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
        keywords_file=os.path.join(base_dir, "keywords.txt"),
        num_threads=2,
        provider="cpu",
        keywords_score=score,
        keywords_threshold=threshold,
    )

def load_wav(filename: str):
    with wave.open(filename, 'rb') as wf:
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return sample_rate, samples

def evaluate_file(kws, samples, sample_rate, chunk_size=1600):
    stream = kws.create_stream()
    detected_count = 0

    # Simulate streaming
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i+chunk_size]
        stream.accept_waveform(sample_rate, chunk)
        while kws.is_ready(stream):
            kws.decode_stream(stream)
            result = kws.get_result(stream)
            if result:
                detected_count += 1
                kws.reset_stream(stream)

    stream.input_finished()
    while kws.is_ready(stream):
        kws.decode_stream(stream)
        result = kws.get_result(stream)
        if result:
            detected_count += 1
            kws.reset_stream(stream)

    return detected_count

def main():
    parser = argparse.ArgumentParser(description="Offline KWS benchmark tool")
    parser.add_argument("--dataset", type=str, default="wakeword_benchmark_dataset", help="Dataset directory")
    parser.add_argument("--scores", type=float, nargs="+", default=[1.0, 1.5, 2.0, 2.5], help="Scores to test")
    parser.add_argument("--thresholds", type=float, nargs="+", default=[0.10, 0.15, 0.20, 0.25, 0.30], help="Thresholds to test")
    args = parser.parse_args()

    dataset_dir = args.dataset
    if not os.path.exists(dataset_dir):
        print(f"Error: Dataset directory '{dataset_dir}' not found.")
        print("Please run record_benchmark_dataset.py first to create the dataset.")
        sys.exit(1)

    # Gather all WAV files
    wav_files = []
    for root, _, files in os.walk(dataset_dir):
        for f in files:
            if f.endswith('.wav'):
                cat = os.path.basename(root)
                wav_files.append({"path": os.path.join(root, f), "category": cat, "filename": f})

    if not wav_files:
        print(f"Error: No WAV files found in '{dataset_dir}'.")
        sys.exit(1)

    trials_csv = "wakeword_benchmark_trials.csv"
    summary_csv = "wakeword_benchmark_summary.csv"

    trials_data = []
    summary_data = []

    print(f"Loaded {len(wav_files)} WAV files.")
    print(f"Testing {len(args.scores)} scores x {len(args.thresholds)} thresholds = {len(args.scores)*len(args.thresholds)} combinations.\n")

    for score in args.scores:
        for threshold in args.thresholds:
            print(f"Testing combination: score={score}, threshold={threshold}...")

            # Create a new KWS instance for each parameter combination to ensure a clean state
            kws = create_kws(score, threshold)

            stats = {
                "natural_total": 0, "natural_detected": 0,
                "paused_total": 0, "paused_detected": 0,
                "negative_total": 0, "false_positives": 0,
                "total_positive": 0, "true_positives": 0, "false_negatives": 0
            }

            for file_info in wav_files:
                sample_rate, samples = load_wav(file_info["path"])

                # Each file evaluation uses a fresh stream created via kws.create_stream()
                detected_count = evaluate_file(kws, samples, sample_rate)
                detected = detected_count > 0

                cat = file_info["category"]
                is_positive = cat.startswith("positive")

                if is_positive:
                    result_type = "TP" if detected else "FN"
                    stats["total_positive"] += 1
                    if detected:
                        stats["true_positives"] += 1
                    else:
                        stats["false_negatives"] += 1

                    if cat == "positive_natural":
                        stats["natural_total"] += 1
                        if detected: stats["natural_detected"] += 1
                    elif cat == "positive_paused":
                        stats["paused_total"] += 1
                        if detected: stats["paused_detected"] += 1
                else:
                    result_type = "FP" if detected else "TN"
                    stats["negative_total"] += 1
                    if detected:
                        stats["false_positives"] += 1

                trials_data.append({
                    "score": score,
                    "threshold": threshold,
                    "category": cat,
                    "filename": file_info["filename"],
                    "expected_keyword": str(is_positive).lower(),
                    "detected": str(detected).lower(),
                    "detections_count": detected_count,
                    "result": result_type
                })

            natural_rate = stats["natural_detected"] / stats["natural_total"] if stats["natural_total"] else 0.0
            paused_rate = stats["paused_detected"] / stats["paused_total"] if stats["paused_total"] else 0.0
            fp_rate = stats["false_positives"] / stats["negative_total"] if stats["negative_total"] else 0.0

            # precision / recall / f1
            tp = stats["true_positives"]
            fp = stats["false_positives"]
            fn = stats["false_negatives"]

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

            summary_data.append({
                "score": score,
                "threshold": threshold,
                "natural_total": stats["natural_total"],
                "natural_detected": stats["natural_detected"],
                "natural_detection_rate": natural_rate,
                "paused_total": stats["paused_total"],
                "paused_detected": stats["paused_detected"],
                "paused_detection_rate": paused_rate,
                "negative_total": stats["negative_total"],
                "false_positives": stats["false_positives"],
                "false_positive_rate": fp_rate,
                "total_positive": stats["total_positive"],
                "true_positives": stats["true_positives"],
                "false_negatives": stats["false_negatives"],
                "precision": precision,
                "recall": recall,
                "f1": f1
            })

    # Write trials CSV
    with open(trials_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["score", "threshold", "category", "filename", "expected_keyword", "detected", "detections_count", "result"])
        writer.writeheader()
        writer.writerows(trials_data)

    # Write summary CSV
    with open(summary_csv, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ["score", "threshold", "natural_total", "natural_detected", "natural_detection_rate",
                      "paused_total", "paused_detected", "paused_detection_rate", "negative_total",
                      "false_positives", "false_positive_rate", "total_positive", "true_positives",
                      "false_negatives", "precision", "recall", "f1"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_data)

    print(f"\nResults saved to {trials_csv} and {summary_csv}")

    # Top 3 Logic
    # 1. natural_detection_rate (DESC)
    # 2. false_positive_rate (ASC)
    # 3. f1 (DESC)
    summary_data.sort(key=lambda x: (x["natural_detection_rate"], -x["false_positive_rate"], x["f1"]), reverse=True)

    print("\n========================================")
    print(" Top 3 Parameter Candidates")
    print("========================================")
    for i, row in enumerate(summary_data[:3]):
        nat_pct = int(row['natural_detection_rate'] * 100)
        pau_pct = int(row['paused_detection_rate'] * 100)
        fp_pct = int(row['false_positive_rate'] * 100)

        print(f"#{i+1} score={row['score']} threshold={row['threshold']}")
        print(f"   natural detection: {row['natural_detected']}/{row['natural_total']} ({nat_pct}%)")
        print(f"   paused detection: {row['paused_detected']}/{row['paused_total']} ({pau_pct}%)")
        print(f"   false positives: {row['false_positives']}/{row['negative_total']} ({fp_pct}%)")
        print(f"   F1-score: {row['f1']:.3f}\n")

if __name__ == "__main__":
    main()
