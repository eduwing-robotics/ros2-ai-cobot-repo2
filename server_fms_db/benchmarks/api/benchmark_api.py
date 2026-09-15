"""
API Benchmark Tool for Baseline Measurement.
"""

import argparse
import asyncio
import csv
import datetime
import json
import os
import platform
import subprocess
import sys
import time

import httpx
import numpy as np


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


async def send_request(client: httpx.AsyncClient, url: str, idx: int, timeout: float):
    start = time.perf_counter()
    try:
        response = await client.get(url, timeout=timeout)
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "request_index": idx,
            "status_code": response.status_code,
            "success": 200 <= response.status_code < 300,
            "latency_ms": elapsed,
            "error": ""
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "request_index": idx,
            "status_code": 0,
            "success": False,
            "latency_ms": elapsed,
            "error": type(e).__name__ + ": " + str(e)
        }


async def worker(client: httpx.AsyncClient, url: str, queue: asyncio.Queue, results: list, timeout: float):
    while True:
        idx = await queue.get()
        if idx is None:
            queue.task_done()
            break

        res = await send_request(client, url, idx, timeout)
        results.append(res)
        queue.task_done()


async def run_load(client: httpx.AsyncClient, url: str, num_requests: int, concurrency: int, timeout: float):
    queue = asyncio.Queue()
    results = []

    for i in range(num_requests):
        queue.put_nowait(i)

    start_time = time.perf_counter()

    tasks = []
    for _ in range(concurrency):
        tasks.append(asyncio.create_task(worker(client, url, queue, results, timeout)))

    await queue.join()

    for _ in range(concurrency):
        queue.put_nowait(None)
    await asyncio.gather(*tasks)

    elapsed_sec = time.perf_counter() - start_time
    results.sort(key=lambda x: x["request_index"])
    return results, elapsed_sec


def compute_stats(results, elapsed_sec):
    latencies = [r["latency_ms"] for r in results]
    success_count = sum(1 for r in results if r["success"])
    error_count = len(results) - success_count

    if not latencies:
        return {}

    stats = {
        "total_requests": len(results),
        "success_count": success_count,
        "error_count": error_count,
        "success_rate": success_count / len(results),
        "elapsed_sec": elapsed_sec,
        "requests_per_sec": len(results) / elapsed_sec if elapsed_sec > 0 else 0,
        "mean_ms": np.mean(latencies),
        "p50_ms": np.percentile(latencies, 50),
        "p95_ms": np.percentile(latencies, 95),
        "p99_ms": np.percentile(latencies, 99),
        "min_ms": np.min(latencies),
        "max_ms": np.max(latencies)
    }
    return stats


async def main():
    parser = argparse.ArgumentParser(description="FMS API Benchmark Tool")
    parser.add_argument("--base-url", type=str, default="http://127.0.0.1:8000", help="Base URL of the API Server")
    parser.add_argument("--label", type=str, default="baseline", help="Label for this benchmark run")
    parser.add_argument("--requests", type=int, default=200, help="Number of requests per test")
    parser.add_argument("--warmup", type=int, default=20, help="Number of warmup requests per test")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 5, 10], help="Concurrency levels to test")
    parser.add_argument("--job-id", type=int, default=None, help="Explicit Job ID to use for detail benchmark")
    parser.add_argument("--timeout", type=float, default=5.0, help="Request timeout in seconds")
    parser.add_argument("--out-dir", type=str, default="benchmarks/api/results", help="Directory to store results")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{args.label}_{timestamp}"

    # Verify Server Connectivity
    print(f"Checking API Server connectivity at {args.base_url} ...")
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=5.0) as client:
            resp = await client.get("/production/jobs")
            if resp.status_code >= 500:
                print(f"Error: Server responded with status {resp.status_code}")
                sys.exit(1)
            jobs = resp.json()
    except Exception as e:
        print(f"API Server is not reachable at {args.base_url}")
        print(f"Error: {e}")
        print("\nPlease make sure the API Server is running. (e.g., uvicorn api_server.main:app)")
        sys.exit(1)

    print("Connectivity OK.\n")

    endpoints_to_test = ["/production/jobs"]

    # Determine Job ID for detail benchmark
    target_job_id = args.job_id
    if target_job_id is None:
        if isinstance(jobs, list) and len(jobs) > 0:
            target_job_id = jobs[0].get("job_id")

    if target_job_id is not None:
        endpoints_to_test.append(f"/production/jobs/{target_job_id}")
    else:
        print("No production job found. Detail benchmark (/production/jobs/{id}) will be skipped.")
        print("Run demo job script to create demo data if needed.\n")

    metadata = {
        "run_id": run_id,
        "measured_at": datetime.datetime.now().isoformat(),
        "git_commit": get_git_commit(),
        "python_version": sys.version.replace("\n", ""),
        "os": platform.platform(),
        "cpu": get_cpu_info(),
        "base_url": args.base_url,
        "requests_per_test": args.requests,
        "warmup_requests": args.warmup,
        "concurrencies": args.concurrency,
        "timeout_sec": args.timeout,
        "endpoints": endpoints_to_test
    }

    metadata_path = os.path.join(args.out_dir, f"api_benchmark_metadata_{timestamp}.json")
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=4)

    summary_csv_path = os.path.join(args.out_dir, f"api_benchmark_summary_{timestamp}.csv")
    trials_csv_path = os.path.join(args.out_dir, f"api_benchmark_trials_{timestamp}.csv")

    summary_fields = ["run_id", "measured_at", "base_url", "endpoint", "concurrency",
                      "total_requests", "success_count", "error_count", "success_rate",
                      "mean_ms", "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms",
                      "elapsed_sec", "requests_per_sec"]

    trials_fields = ["run_id", "timestamp", "endpoint", "concurrency", "request_index",
                     "status_code", "success", "latency_ms", "error"]

    with open(summary_csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()

    with open(trials_csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=trials_fields)
        writer.writeheader()

    print("====================================================")
    print("API Benchmark Result")
    print("====================================================\n")

    async with httpx.AsyncClient(base_url=args.base_url) as client:
        for ep in endpoints_to_test:
            for c in args.concurrency:
                # Warm-up
                if args.warmup > 0:
                    await run_load(client, ep, args.warmup, c, args.timeout)

                # Benchmark
                results, elapsed = await run_load(client, ep, args.requests, c, args.timeout)
                stats = compute_stats(results, elapsed)

                # Output to console
                print(f"GET {ep}")
                print(f"Concurrency: {c}\n")
                print(f"Requests       : {stats['total_requests']}")
                success_pct = (stats['success_rate'] * 100)
                print(f"Success        : {stats['success_count']} / {stats['total_requests']} ({success_pct:.1f}%)")
                print(f"Mean           : {stats['mean_ms']:.2f} ms")
                print(f"P50            : {stats['p50_ms']:.2f} ms")
                print(f"P95            : {stats['p95_ms']:.2f} ms")
                print(f"P99            : {stats['p99_ms']:.2f} ms")
                print(f"RPS            : {stats['requests_per_sec']:.1f} req/s")
                print("\n----------------------------------------------------\n")

                # Append to Summary CSV
                row_summary = {
                    "run_id": run_id,
                    "measured_at": metadata["measured_at"],
                    "base_url": args.base_url,
                    "endpoint": ep,
                    "concurrency": c,
                    **stats
                }

                with open(summary_csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=summary_fields)
                    writer.writerow(row_summary)

                # Append to Trials CSV
                with open(trials_csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=trials_fields)
                    for r in results:
                        writer.writerow({
                            "run_id": run_id,
                            "timestamp": metadata["measured_at"],
                            "endpoint": ep,
                            "concurrency": c,
                            "request_index": r["request_index"],
                            "status_code": r["status_code"],
                            "success": str(r["success"]).lower(),
                            "latency_ms": r["latency_ms"],
                            "error": r["error"]
                        })

    print("====================================================")
    print(f"Benchmark completed successfully.")
    print(f"Metadata : {metadata_path}")
    print(f"Summary  : {summary_csv_path}")
    print(f"Trials   : {trials_csv_path}")
    print("====================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
