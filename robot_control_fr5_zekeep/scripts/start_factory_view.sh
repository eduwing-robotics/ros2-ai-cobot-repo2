#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

set +u                      # ROS setup.bash 가 set -u 에서 죽는다(AMENT_TRACE_SETUP_FILES)
source /opt/ros/jazzy/setup.bash

# Robot Control PC operational ROS domain.
export ROS_DOMAIN_ID=90

# If FR5 workspace exists, source it without requiring it.
if [ -f "$HOME/fr5_ros2_ws/install/setup.bash" ]; then
    source "$HOME/fr5_ros2_ws/install/setup.bash"
fi
set -u

CAMERA="/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0"
UNITY_HOST="192.168.20.29"
UNITY_PORT="21030"
TOPIC="/vision/factory_camera/image_view"

mkdir -p logs

echo "============================================================"
echo "HARMONY FACTORY VIEW - ROBOT CONTROL PC"
echo "============================================================"
echo "ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"
echo "CAMERA=${CAMERA}"
echo "TOPIC=${TOPIC}"
echo "UNITY=${UNITY_HOST}:${UNITY_PORT}"
echo

if [ ! -e "$CAMERA" ]; then
    echo "ABORT: C270 stable device not found:"
    echo "  $CAMERA"
    exit 1
fi

for CMD in python3 ffmpeg v4l2-ctl; do
    if ! command -v "$CMD" >/dev/null 2>&1; then
        echo "ABORT: required command missing: $CMD"
        exit 1
    fi
done

# Avoid duplicate local copies.
pkill -f \
  'scripts/network/harmony_factory_camera_publisher_v1.py' \
  2>/dev/null || true

pkill -f \
  'scripts/network/harmony_unity_video_udp_v3.py.*--stream factory' \
  2>/dev/null || true

sleep 1

nohup python3 \
  scripts/network/harmony_factory_camera_publisher_v1.py \
  > logs/factory_camera_publisher.log \
  2>&1 < /dev/null &

PUB_PID=$!

echo "FACTORY_PUBLISHER_PID=${PUB_PID}"

sleep 3

if ! kill -0 "$PUB_PID" 2>/dev/null; then
    echo "ABORT: Factory Publisher exited"
    tail -n 100 logs/factory_camera_publisher.log
    exit 1
fi

nohup python3 \
  scripts/network/harmony_unity_video_udp_v3.py \
  --stream factory \
  --host "$UNITY_HOST" \
  --port "$UNITY_PORT" \
  --topic "$TOPIC" \
  --fps 10 \
  --quality 80 \
  > logs/unity_factory_udp_21030.log \
  2>&1 < /dev/null &

UNITY_PID=$!

echo "UNITY_FACTORY_PID=${UNITY_PID}"

sleep 3

if ! kill -0 "$UNITY_PID" 2>/dev/null; then
    echo "ABORT: Unity Factory sender exited"
    tail -n 100 logs/unity_factory_udp_21030.log
    exit 1
fi

echo
echo "FACTORY_VIEW_START=PASS"
echo
echo "Open another terminal and run:"
echo "  ./verify_factory_view.sh"
