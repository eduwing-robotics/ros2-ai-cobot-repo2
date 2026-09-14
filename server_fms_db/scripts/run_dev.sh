#!/usr/bin/env bash
set -euo pipefail

if [[ "$(basename "$PWD")" != "backend" ]]; then
  echo "Run this script from the backend directory."
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo ".venv was not found. Create it before running the development services."
  exit 1
fi

source .venv/bin/activate
pids=()

cleanup() {
  echo "Stopping development services..."
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait "${pids[@]:-}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

uvicorn api_server.main:app --reload --host 0.0.0.0 --port 8000 &
pids+=("$!")
uvicorn telemetry_gateway.main:app --reload --host 0.0.0.0 --port 8001 &
pids+=("$!")
python -m fms_server.main &
pids+=("$!")

echo "API Server: http://localhost:8000/docs"
echo "Telemetry Gateway: http://localhost:8001/health"
echo "Press Ctrl+C to stop all services."
wait
