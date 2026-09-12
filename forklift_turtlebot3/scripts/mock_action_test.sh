#!/usr/bin/env bash
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROS_DOMAIN_ID=98
source /opt/ros/jazzy/setup.bash
source "${PROJECT_DIR}/install/setup.bash"

ros2 launch forklift_control forklift_pi.launch.py \
  enable_manual_websocket:=false > /tmp/forklift_mock_pi.log 2>&1 &
PI_LAUNCH_PID=$!

ros2 launch forklift_control forklift.launch.py \
  enable_action_server:=true \
  use_mock_route:=true \
  use_mock_docking:=true \
  use_mock_lift:=false > /tmp/forklift_mock_action.log 2>&1 &
LAUNCH_PID=$!

ros2 run forklift_control lift_servo \
  --ros-args -p use_mock_lift:=true > /tmp/forklift_mock_lift.log 2>&1 &
LIFT_PID=$!

cleanup() {
  kill -TERM "${LIFT_PID}" 2>/dev/null || true
  wait "${LIFT_PID}" 2>/dev/null || true
  kill -TERM "${LAUNCH_PID}" 2>/dev/null || true
  wait "${LAUNCH_PID}" 2>/dev/null || true
  kill -TERM "${PI_LAUNCH_PID}" 2>/dev/null || true
  wait "${PI_LAUNCH_PID}" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  if ros2 action list | grep -qx '/forklift/execute_transport'; then
    break
  fi
  sleep 0.2
done

send_transport() {
  local req_id=$1
  local job_id=$2
  local delivery_id=$3
  local pickup_code=$4
  local dropoff_code=$5
  ros2 action send_goal /forklift/execute_transport \
    forklift_interfaces/action/ExecuteTransport \
    "{req_id: '${req_id}', job_id: ${job_id}, delivery_id: ${delivery_id}, pickup_code: '${pickup_code}', dropoff_code: '${dropoff_code}'}" \
    --feedback
}

send_transport mock-rack1-drop 1 1 RACK1 DROP
send_transport mock-drop-rack1 1 2 DROP RACK1
send_transport mock-rack2-drop 1 3 RACK2 DROP
send_transport mock-drop-rack2 1 4 DROP RACK2

ros2 action send_goal /forklift/return_home \
  forklift_interfaces/action/ReturnHome \
  '{req_id: "mock-home"}' --feedback
