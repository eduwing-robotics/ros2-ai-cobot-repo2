#!/usr/bin/env bash
# fr5_home.sh — FR5 를 home 자세로 보낸다. 필요하면 관절 한계 탈출까지 알아서 한다.
#
#   ~/fr5_home.sh          # 확인만 (움직이지 않음)
#   ~/fr5_home.sh --go     # 실제 이동
#
# ★2026-08-19: 팔이 j1=-179.53° (URDF 한계 ±175° 밖) 에 세워져 있으면 MoveIt 이
#   START_STATE_INVALID 로 계획을 거부한다. 그래서 ①컨트롤러 직접 궤적으로 한계
#   안쪽까지만 최소 이동 → ②MoveIt 정상 경로로 home. ①은 충돌검사가 없으므로
#   반드시 최소한만.

set -e

# ROS 환경 (bashrc 는 도메인 97 이라 팀망 73 으로 명시 전환)
set +u
source /opt/ros/jazzy/setup.bash
source "$HOME/fr5_jazzy_test_ws/install/setup.bash"
source "$HOME/team_env.sh" >/dev/null
set -u

GO=""
[ "${1:-}" = "--go" ] && GO="--go"

echo "═══ 1) 관절 한계 확인 ═══"
python3 "$HOME/fr5_nudge_inbounds.py" $GO 2>/dev/null

if [ -z "$GO" ]; then
    echo
    echo "확인 모드입니다. 실제로 보내려면:  ~/fr5_home.sh --go"
    exit 0
fi

echo
echo "═══ 2) MoveIt 으로 home 이동 (충돌검사 포함) ═══"
python3 "$HOME/fr5_pose.py" goto home 2>/dev/null

echo
echo "═══ 3) 도달 확인 ═══"
python3 "$HOME/fr5_pose.py" show 2>/dev/null
