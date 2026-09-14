#!/usr/bin/env bash
# Start only the Unity telemetry path: Redis (reused), Gateway, and API.

# ROS setup scripts reference optional variables while expanding their hooks.
# Enable nounset only after sourcing them.
set -eo pipefail

readonly MAIN_SERVER_IP="${MAIN_SERVER_IP:-192.168.20.20}"
readonly ROS_SETUP="/opt/ros/jazzy/setup.bash"
readonly ROS_WORKSPACE_SETUP="${HOME}/factory_ros_ws/install/setup.bash"
readonly API_PORT=8000
readonly GATEWAY_PORT=8001

owned_gateway_pid=""
owned_api_pid=""
cleanup_done=false

info() { printf '%s\n' "$*"; }
error() { printf '[ERROR] %s\n' "$*" >&2; }

require_file() {
  if [[ ! -f "$1" ]]; then
    error "$2"
    exit 1
  fi
}

health_json() {
  curl --fail --silent --show-error --max-time 2 "$1/health"
}

wait_for_health() {
  local name="$1"
  local base_url="$2"
  local expected_service="$3"
  local pid="$4"
  local attempt response

  for attempt in {1..20}; do
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      error "$name exited during startup."
      return 1
    fi
    response="$(health_json "$base_url" 2>/dev/null || true)"
    if [[ "$response" == *"\"service\":\"$expected_service\""* ]]; then
      return 0
    fi
    sleep 0.25
  done
  error "$name did not become healthy at $base_url/health."
  return 1
}

port_pid() {
  local port="$1"
  ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1
}

stop_owned_process() {
  local name="$1"
  local pid="$2"
  local attempt
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0

  info "[STOP] $name pid=$pid"
  kill -TERM "$pid" 2>/dev/null || true
  for attempt in {1..20}; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.25
  done
  error "$name pid=$pid did not stop after TERM; sending KILL."
  kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  [[ "$cleanup_done" == true ]] && return
  cleanup_done=true
  stop_owned_process "API Server" "$owned_api_pid"
  stop_owned_process "Telemetry Gateway" "$owned_gateway_pid"
  wait "${owned_api_pid:-}" "${owned_gateway_pid:-}" 2>/dev/null || true
}

on_signal() {
  cleanup
  exit 0
}

trap cleanup EXIT
trap on_signal INT TERM

if [[ "$(basename "$PWD")" != "backend" ]]; then
  error "Run this script from the backend directory: cd ~/project2/backend"
  exit 1
fi

require_file "$ROS_SETUP" "ROS Jazzy setup was not found: $ROS_SETUP"
require_file "$ROS_WORKSPACE_SETUP" "factory_ros_ws setup was not found: $ROS_WORKSPACE_SETUP"
require_file ".venv/bin/activate" ".venv not found. Create it before running this stack."

info "[CHECK] Redis ..."
if ! command -v redis-cli >/dev/null 2>&1 || ! redis-cli ping 2>/dev/null | grep -qx 'PONG'; then
  error "Redis is not running. Start Redis first: sudo systemctl start redis-server"
  exit 1
fi
info "[CHECK] Redis ... OK"

info "[CHECK] ROS Jazzy ..."
source "$ROS_SETUP"
info "[CHECK] ROS Jazzy ... OK"

info "[CHECK] factory_ros_ws ..."
source "$ROS_WORKSPACE_SETUP"
set -u
info "[CHECK] factory_ros_ws ... OK"

info "[CHECK] Backend venv ..."
source .venv/bin/activate
info "[CHECK] Backend venv ... OK"

export ROS_DOMAIN_ID=73
export TELEMETRY_ROS_ENABLED=true
info "[INFO] ROS_DOMAIN_ID=$ROS_DOMAIN_ID"
[[ -n "${RMW_IMPLEMENTATION:-}" ]] && info "[INFO] RMW_IMPLEMENTATION=$RMW_IMPLEMENTATION"
[[ -n "${CYCLONEDDS_URI:-}" ]] && info "[INFO] CYCLONEDDS_URI=$CYCLONEDDS_URI"

mkdir -p logs/runtime

api_existing_pid="$(port_pid "$API_PORT")"
if [[ -n "$api_existing_pid" ]]; then
  if wait_for_health "Existing API Server" "http://127.0.0.1:$API_PORT" "api-server" ""; then
    info "[USE] Existing API Server pid=$api_existing_pid"
  else
    error "Port $API_PORT is already in use by pid=$api_existing_pid, not a healthy API Server. Resolve the conflict manually."
    exit 1
  fi
else
  info "[START] API Server"
  python -m uvicorn api_server.main:app --host 0.0.0.0 --port "$API_PORT" > logs/runtime/api_server.log 2>&1 &
  owned_api_pid=$!
  if ! wait_for_health "API Server" "http://127.0.0.1:$API_PORT" "api-server" "$owned_api_pid"; then
    exit 1
  fi
fi

gateway_existing_pid="$(port_pid "$GATEWAY_PORT")"
if [[ -n "$gateway_existing_pid" ]]; then
  gateway_health="$(health_json "http://127.0.0.1:$GATEWAY_PORT" 2>/dev/null || true)"
  if [[ "$gateway_health" == *'"service":"telemetry-gateway"'* && "$gateway_health" == *'"ros":"running"'* ]]; then
    info "[USE] Existing Telemetry Gateway pid=$gateway_existing_pid"
  else
    error "Port $GATEWAY_PORT is already in use by pid=$gateway_existing_pid, not a healthy ROS-enabled Telemetry Gateway. Resolve the conflict manually."
    exit 1
  fi
else
  info "[START] Telemetry Gateway"
  python -m uvicorn telemetry_gateway.main:app --host 0.0.0.0 --port "$GATEWAY_PORT" > logs/runtime/telemetry_gateway.log 2>&1 &
  owned_gateway_pid=$!
  if ! wait_for_health "Telemetry Gateway" "http://127.0.0.1:$GATEWAY_PORT" "telemetry-gateway" "$owned_gateway_pid"; then
    exit 1
  fi
  gateway_health="$(health_json "http://127.0.0.1:$GATEWAY_PORT")"
  if [[ "$gateway_health" != *'"ros":"running"'* ]]; then
    error "Telemetry Gateway started but ROS subscriptions are not running. See logs/runtime/telemetry_gateway.log"
    exit 1
  fi
fi

info ""
info "========================================"
info " Unity Backend Stack READY"
info "========================================"
info ""
info "Main Server:"
info "$MAIN_SERVER_IP"
info ""
info "Unity WebSocket:"
info "ws://$MAIN_SERVER_IP:$API_PORT/ws/unity"
info ""
info "API:"
info "http://$MAIN_SERVER_IP:$API_PORT"
info ""
info "ROS_DOMAIN_ID:"
info "$ROS_DOMAIN_ID"
info ""
info "Telemetry ROS:"
info "ENABLED"
info ""
info "Redis:"
info "CONNECTED"
info ""
info "========================================"
info "Press Ctrl+C to stop owned processes."
info "========================================"

while true; do
  if [[ -n "$owned_api_pid" ]] && ! kill -0 "$owned_api_pid" 2>/dev/null; then
    error "API Server exited unexpectedly."
    exit 1
  fi
  if [[ -n "$owned_gateway_pid" ]] && ! kill -0 "$owned_gateway_pid" 2>/dev/null; then
    error "Telemetry Gateway exited unexpectedly."
    exit 1
  fi
  sleep 1
done
