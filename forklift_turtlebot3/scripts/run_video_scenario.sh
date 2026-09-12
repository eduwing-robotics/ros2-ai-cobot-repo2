#!/usr/bin/env bash
# ROS 2 setup scripts may read optional variables that are not defined yet, so
# enable nounset only after both environments have been sourced.
set -eo pipefail

scenario="${1:-}"
if [[ "$scenario" != "1" && "$scenario" != "2" && "$scenario" != "3" ]]; then
  echo "사용법: $0 {1|2|3}" >&2
  exit 2
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$project_dir/install/setup.bash"
set -u
export ROS_DOMAIN_ID=73
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
unset ROS_STATIC_PEERS
dds_pc_path="$(ros2 pkg prefix --share forklift_control)/config/cyclonedds_pc_unicast.xml"
export CYCLONEDDS_URI="file://$dds_pc_path"

log_file="/tmp/forklift_video_scenario_${$}.log"
cleanup() {
  rm -f "$log_file"
}
trap cleanup EXIT

run_transport() {
  local req_id="$1" job_id="$2" delivery_id="$3" pickup="$4" dropoff="$5"
  : > "$log_file"
  ros2 action send_goal /forklift/execute_transport \
    forklift_interfaces/action/ExecuteTransport \
    "{req_id: '$req_id', job_id: $job_id, delivery_id: $delivery_id, pickup_code: '$pickup', dropoff_code: '$dropoff'}" \
    --feedback 2>&1 | tee "$log_file"
  if ! grep -q "status: SUCCEEDED" "$log_file"; then
    echo "실패를 감지해 다음 단계를 실행하지 않습니다." >&2
    exit 1
  fi
}

run_home() {
  local req_id="$1"
  : > "$log_file"
  ros2 action send_goal /forklift/return_home \
    forklift_interfaces/action/ReturnHome \
    "{req_id: '$req_id'}" \
    --feedback 2>&1 | tee "$log_file"
  if ! grep -q "status: SUCCEEDED" "$log_file"; then
    echo "HOME 복귀 실패를 감지했습니다." >&2
    exit 1
  fi
}

echo "영상 ${scenario} 시나리오를 5초 후 시작합니다."
for remaining in 5 4 3 2 1; do
  echo "${remaining}..."
  sleep 1
done

case "$scenario" in
  1)
    run_transport video1-rack1-drop 101 1 RACK1 DROP
    ;;
  2)
    run_transport video2-drop-rack1 102 2 DROP RACK1
    run_transport video2-rack2-drop 102 3 RACK2 DROP
    ;;
  3)
    run_transport video3-drop-rack2 103 4 DROP RACK2
    run_home video3-final-return-home
    ;;
esac

echo "영상 ${scenario} 시나리오 전체가 성공했습니다."
