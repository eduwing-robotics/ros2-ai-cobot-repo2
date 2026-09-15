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
TRAILING_BLANKS = 1


def create_kws(keyword_file):
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
        keywords_file=os.path.join(BASE_DIR, keyword_file),
        num_threads=2,
        provider="cpu",
        keywords_score=SCORE,
        keywords_threshold=THRESHOLD,
        num_trailing_blanks=TRAILING_BLANKS,
    )


def load_wav(path):
    with wave.open(path, "rb") as wf:
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    samples = (
        np.frombuffer(frames, dtype=np.int16)
        .astype(np.float32)
        / 32768.0
    )

    return sample_rate, samples


def evaluate(kws, samples, sample_rate, chunk_size=1600):
    stream = kws.create_stream()

    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i + chunk_size]
        stream.accept_waveform(sample_rate, chunk)

        while kws.is_ready(stream):
            kws.decode_stream(stream)

            if kws.get_result(stream):
                return True

    stream.input_finished()

    while kws.is_ready(stream):
        kws.decode_stream(stream)

        if kws.get_result(stream):
            return True

    return False


kws_a = create_kws("keywords_detector_a.txt")
kws_b = create_kws("keywords_detector_b.txt")

files = []

for root, _, names in os.walk(DATASET_DIR):
    for name in names:
        if name.endswith(".wav"):
            files.append({
                "path": os.path.join(root, name),
                "category": os.path.basename(root),
                "filename": name,
            })

files.sort(key=lambda x: (x["category"], x["filename"]))

rows = []

natural_total = natural_detected = 0
paused_total = paused_detected = 0
negative_total = false_positives = 0

natural_failed = []
fp_files = []

for item in files:
    sr, samples = load_wav(item["path"])

    detected_a = evaluate(kws_a, samples, sr)
    detected_b = evaluate(kws_b, samples, sr)

    # Ensemble: 둘 중 하나라도 검출하면 Wake
    detected = detected_a or detected_b

    category = item["category"]

    if category == "positive_natural":
        natural_total += 1

        if detected:
            natural_detected += 1
        else:
            natural_failed.append(item["filename"])

    elif category == "positive_paused":
        paused_total += 1

        if detected:
            paused_detected += 1

    else:
        negative_total += 1

        if detected:
            false_positives += 1
            fp_files.append(item["filename"])

    rows.append({
        "category": category,
        "filename": item["filename"],
        "detector_a": str(detected_a).lower(),
        "detector_b": str(detected_b).lower(),
        "ensemble": str(detected).lower(),
    })


print("========================================")
print(" Dual KWS Ensemble")
print("========================================")

print(
    f"Natural: {natural_detected}/{natural_total} "
    f"({natural_detected / natural_total:.0%})"
)

print(
    f"Paused : {paused_detected}/{paused_total} "
    f"({paused_detected / paused_total:.0%})"
)

print(
    f"FP     : {false_positives}/{negative_total} "
    f"({false_positives / negative_total:.0%})"
)

print("\nNatural failed:")
if natural_failed:
    for x in natural_failed:
        print(" ", x)
else:
    print("  none")

print("\nFalse-positive files:")
if fp_files:
    for x in fp_files:
        print(" ", x)
else:
    print("  none")


RESULT_DIR = os.path.join(BASE_DIR, "benchmark_results")
os.makedirs(RESULT_DIR, exist_ok=True)

output = os.path.join(
    RESULT_DIR,
    "wakeword_dual_kws_ensemble_v1_trials.csv",
)

with open(output, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=[
            "category",
            "filename",
            "detector_a",
            "detector_b",
            "ensemble",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)

print("\nSaved:")
print(output)
