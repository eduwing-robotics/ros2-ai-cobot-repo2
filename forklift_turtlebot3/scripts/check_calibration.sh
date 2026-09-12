#!/usr/bin/env bash
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "${PROJECT_DIR}/install/setup.bash"

ros2 run forklift_control calibration_validator --ros-args \
  -p calibration_file:="${PROJECT_DIR}/src/forklift_control/config/calibration.yaml"
