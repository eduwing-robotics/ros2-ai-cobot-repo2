# Conversation Benchmark v2

This benchmark measures the text-only production conversation flow through the
real `POST /ai/conversation` endpoint: initial EXAONE interpretation, pending
request state, deterministic roof/confirmation follow-ups, and materialized
production jobs.  It is not a voice, FMS, ROS2, or robot benchmark.

`conversation_dataset_v2.json` is the fixed 30-scenario v2 dataset.  After a
formal baseline has been run, do not edit it; create a new dataset version for
new coverage.

## Safe execution

The target database must be a dedicated benchmark database (default:
`smart_factory_benchmark`).  The runner refuses another database name.  Start
an API process configured with that same database, then run:

```bash
BENCHMARK_DATABASE_URL='postgresql+psycopg://…/smart_factory_benchmark' \
.venv/bin/python benchmarks/conversation/benchmark_conversation.py \
  --base-url http://127.0.0.1:8002 --repeats 3 \
  --label conversation_baseline_v2_20260813
```

The database URL is used only for read-only validation queries and is never
written to artifacts.  The API itself creates benchmark jobs in its isolated
database.  Each scenario has a unique session ID, so scenario data is not
reused.

## Measurements

The runner performs three non-scoring preflight scenarios, then executes all
30 scenarios for each repeat.  It records turn-level HTTP/state/command
results, scenario-level job materialization checks, exact deterministic
messages, safety violations, stability, and separate initial/follow-up/total
latencies.

A safety violation is any scenario that creates more jobs than expected,
including jobs after rejection, invalid input, no pending request, or a
duplicate confirmation.  Generated artifacts are timestamped under
`benchmarks/conversation/results/` and are never overwritten.
