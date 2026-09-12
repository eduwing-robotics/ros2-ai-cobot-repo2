#!/usr/bin/env bash
set -eo pipefail

PI_TARGET="${1:-pi@192.168.20.100}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_SRC="/home/pi/forklift_ws/src"
REMOTE_LIFT="/home/pi/forklift"

echo "Deploy target: ${PI_TARGET}"
ssh "${PI_TARGET}" "mkdir -p '${REMOTE_SRC}' '${REMOTE_LIFT}'"
scp -r \
  "${PROJECT_DIR}/src/forklift_interfaces" \
  "${PROJECT_DIR}/src/forklift_control" \
  "${PI_TARGET}:${REMOTE_SRC}/"

scp \
  "${PROJECT_DIR}/src/forklift_control/config/cyclonedds_pi_unicast.xml" \
  "${PI_TARGET}:/home/pi/cyclonedds_unicast.xml"

scp \
  "${PROJECT_DIR}/hardware/lift/pi/fk_jogd.py" \
  "${PROJECT_DIR}/hardware/lift/pi/forklift_pi.py" \
  "${PROJECT_DIR}/hardware/lift/pi/jogd_start" \
  "${PROJECT_DIR}/hardware/lift/pi/jogd_stop" \
  "${PROJECT_DIR}/hardware/lift/pi/setup.sh" \
  "${PI_TARGET}:${REMOTE_LIFT}/"

ssh -t "${PI_TARGET}" \
  "chmod +x /home/pi/forklift/jogd_start /home/pi/forklift/jogd_stop /home/pi/forklift/setup.sh && cd /home/pi/forklift_ws && source /opt/ros/jazzy/setup.bash && colcon build --packages-select forklift_interfaces forklift_control --symlink-install"

echo "기존 /home/pi/.forklift_pi.json은 변경하지 않았습니다."
echo "배포 완료. Pi에서 ROS_DOMAIN_ID=73과 /home/pi/cyclonedds_unicast.xml을 사용하세요."
