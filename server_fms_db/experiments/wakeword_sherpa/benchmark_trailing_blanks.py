import csv
import os
import wave

import numpy as np
import sherpa_onnx


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(
    BASE_DIR,
    "models",
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
)

DATASET_DIR = os.path.join(
    BASE_DIR,
    "wakeword_benchmark_dataset_quiet_baseline_v1",
)

SCORE = 3.0
THRESHOLD = 0.10
TRAILING_BLANKS_VALUES = [1, 4, 8]


def create_kws(trailing_blanks: int):
    return sherpa_onnx.KeywordSpotter(
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        encoder=os.path.join(
            MODEL_DIR,
            "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
        decoder=os.path.join(
            MODEL_DIR,
            "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
        joiner=os.path.join(
            MODEL_DIR,
            "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
        keywords_file=os.path.join(BASE_DIR, "keywords.txt"),
        num_threads=2,
        provider="cpu",
        keywords_score=SCORE,
        keywords_threshold=THRESHOLD,
        num_trailing_blanks=trailing_blanks,
    )


def load_wav(filename):
    with wave.open(filename, "rb") as wf:
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    samples = (
        np.frombuffer(frames, dtype=np.int16)
        .astype(np.float32)
        / 32768.0
    )
    return sample_rate, samples


def evaluate_file(kws, samples, sample_rate, chunk_size=1600):
    stream = kws.create_stream()
    detected_count = 0

    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i + chunk_size]
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


wav_files = []

for root, _, files in os.walk(DATASET_DIR):
    for filename in files:
        if filename.endswith(".wav"):
            wav_files.append({
                "path": os.path.join(root, filename),
                "category": os.path.basename(root),
                "filename": filename,
            })

wav_files.sort(key=lambda x: (x["category"], x["filename"]))

print(f"Loaded {len(wav_files)} WAV files.")
print(f"score={SCORE}, threshold={THRESHOLD}")
print(f"trailing blanks={TRAILING_BLANKS_VALUES}\n")

summary_rows = []
trial_rows = []

for trailing_blanks in TRAILING_BLANKS_VALUES:
    print(f"Testing num_trailing_blanks={trailing_blanks}...")

    kws = create_kws(trailing_blanks)

    natural_total = 0
    natural_detected = 0
    paused_total = 0
    paused_detected = 0
    negative_total = 0
    false_positives = 0

    failed_natural = []

    for item in wav_files:
        sample_rate, samples = load_wav(item["path"])
        detected_count = evaluate_file(kws, samples, sample_rate)
        detected = detected_count > 0

        category = item["category"]

        if category == "positive_natural":
            natural_total += 1
            if detected:
                natural_detected += 1
            else:
                failed_natural.append(item["filename"])

        elif category == "positive_paused":
            paused_total += 1
            if detected:
                paused_detected += 1

        else:
            negative_total += 1
            if detected:
                false_positives += 1

        trial_rows.append({
            "score": SCORE,
            "threshold": THRESHOLD,
            "trailing_blanks": trailing_blanks,
            "category": category,
            "filename": item["filename"],
            "detected": str(detected).lower(),
            "detections_count": detected_count,
        })

    natural_rate = (
        natural_detected / natural_total if natural_total else 0
    )
    paused_rate = (
        paused_detected / paused_total if paused_total else 0
    )
    fp_rate = (
        false_positives / negative_total if negative_total else 0
    )

    summary_rows.append({
        "score": SCORE,
        "threshold": THRESHOLD,
        "trailing_blanks": trailing_blanks,
        "natural_total": natural_total,
        "natural_detected": natural_detected,
        "natural_detection_rate": natural_rate,
        "paused_total": paused_total,
        "paused_detected": paused_detected,
        "paused_detection_rate": paused_rate,
        "negative_total": negative_total,
        "false_positives": false_positives,
        "false_positive_rate": fp_rate,
        "failed_natural": ";".join(sorted(failed_natural)),
    })

    print(
        f"  Natural: {natural_detected}/{natural_total} "
        f"({natural_rate:.0%})"
    )
    print(
        f"  Paused : {paused_detected}/{paused_total} "
        f"({paused_rate:.0%})"
    )
    print(
        f"  FP     : {false_positives}/{negative_total} "
        f"({fp_rate:.0%})"
    )
    print(
        "  Failed : "
        + (", ".join(sorted(failed_natural))
           if failed_natural else "none")
    )
    print()


RESULT_DIR = os.path.join(BASE_DIR, "benchmark_results")
os.makedirs(RESULT_DIR, exist_ok=True)

summary_path = os.path.join(
    RESULT_DIR,
    "wakeword_trailing_blanks_ablation_v1_summary.csv",
)

trials_path = os.path.join(
    RESULT_DIR,
    "wakeword_trailing_blanks_ablation_v1_trials.csv",
)

with open(summary_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=list(summary_rows[0].keys()),
    )
    writer.writeheader()
    writer.writerows(summary_rows)

with open(trials_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=list(trial_rows[0].keys()),
    )
    writer.writeheader()
    writer.writerows(trial_rows)

print("Saved:")
print(summary_path)
print(trials_path)
