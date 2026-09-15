#!/bin/bash
# move_all_three.sh — FR5 + ZK1 + ZK2 를 진짜 동시에 구동한다.
#
#   ./move_all_three.sh [FR5_j1_delta] [ZK1_a1_delta] [ZK2_a1_delta] [속도%]
#   예) ./move_all_three.sh 20 20 20
#
# ★ 왜 이 스크립트가 필요한가 (2026-08-08 교훈)
#   셸의 "병렬" 호출이 실제로는 순차 실행되는 환경이 있다. 3대가 하나씩
#   움직이는 것을 사용자가 육안으로 적발했다. 진짜 동시 구동은 한 셸에서
#   백그라운드 잡 3개를 띄우고 wait 로 모으는 방법뿐이다.
#
# ★ FR5 는 MoveGroup 이 SUCCESS 를 반환해도 실물은 아직 움직이는 중이다.
#   fr5_goto.py 가 joint_states 안정까지 기다리므로 wait 가 의미를 갖는다.

set -o pipefail
SCRATCH="${SCRATCH_DIR:-/tmp/robot_cell}"
LOG="${SCRATCH}/move3"
mkdir -p "$LOG"

FR5_D="${1:-20}"
ZK1_D="${2:-20}"
ZK2_D="${3:-20}"
SPEED="${4:-10}"          # FR5 속도 % (ZK 는 스크립트 내부 8% 고정)

# ROS 환경 — setup.bash 는 미정의 변수를 참조하므로 set -u 를 켜지 않는다
source /opt/ros/jazzy/setup.bash
source /home/ar/fr5_jazzy_test_ws/install/setup.bash
export ROS_DOMAIN_ID=97
export CYCLONEDDS_URI="file:///home/ar/cyclonedds_localhost.xml"

echo "════════════════════════════════════════════"
echo " 3대 동시 구동"
echo "   FR5 j1 ${FR5_D}°   ZK1 A1 ${ZK1_D}°   ZK2 A1 ${ZK2_D}°"
echo "════════════════════════════════════════════"
T0=$(date +%s.%N)

# ── 백그라운드 3잡 동시 출발 ──
python3 "${SCRATCH}/fr5_goto.py" --joint j1 --delta "$FR5_D" --scale "0.$(printf '%02d' "$SPEED")" \
    > "${LOG}/fr5.log" 2>&1 &
P_FR5=$!

python3 "${SCRATCH}/zk_axis_test.py" --robot zkbot1 --axis A1 --delta "$ZK1_D" \
    > "${LOG}/zk1.log" 2>&1 &
P_ZK1=$!

python3 "${SCRATCH}/zk_axis_test.py" --robot zkbot2 --axis A1 --delta "$ZK2_D" \
    > "${LOG}/zk2.log" 2>&1 &
P_ZK2=$!

echo "  출발: FR5(pid $P_FR5)  ZK1(pid $P_ZK1)  ZK2(pid $P_ZK2)"
echo "  ── 세 대가 동시에 움직입니다 ──"

wait $P_FR5; R_FR5=$?
wait $P_ZK1; R_ZK1=$?
wait $P_ZK2; R_ZK2=$?

T1=$(date +%s.%N)
echo ""
echo "──────── 결과 ────────"
printf "  FR5  %s  %s\n" "$([ $R_FR5 -eq 0 ] && echo ✅ || echo 🔴)" \
    "$(grep '도달' "${LOG}/fr5.log" | tail -1)"
printf "  ZK1  %s  %s\n" "$([ $R_ZK1 -eq 0 ] && echo ✅ || echo 🔴)" \
    "$(grep -E '이동 — 비트|안 움직였다' "${LOG}/zk1.log" | tail -1 | sed 's/^ *//')"
printf "  ZK2  %s  %s\n" "$([ $R_ZK2 -eq 0 ] && echo ✅ || echo 🔴)" \
    "$(grep -E '이동 — 비트|안 움직였다' "${LOG}/zk2.log" | tail -1 | sed 's/^ *//')"
echo ""
printf "  총 소요 %.1f초 (순차였다면 세 대 시간의 합이 나온다)\n" "$(echo "$T1 - $T0" | bc)"
echo "  로그: ${LOG}/{fr5,zk1,zk2}.log"

[ $R_FR5 -eq 0 ] && [ $R_ZK1 -eq 0 ] && [ $R_ZK2 -eq 0 ]
