#!/usr/bin/env bash
# fr5_up.sh — FR5 실기 스택 기동/종료/점검 (매일 아침 고정비용 제거용)
#
#   ~/fr5_up.sh            # 기동 (네트워크 → 개통순서 → real_robot → 검증)
#   ~/fr5_up.sh status     # 현재 상태만 확인 (아무것도 안 건드림)
#   ~/fr5_up.sh down       # 정리 (SDK 소켓 해제까지 확인)
#
# ─────────────────────────────────────────────────────────────────────
# ★ 이 스크립트가 막아주는 함정 (전부 실제로 당한 것)
#
#  1) 2026-08-17 ─ `kill -INT` 대상이 틀렸다
#     `ros2 run ... ros2_cmd_server`(파이썬 래퍼)만 죽고 실제 바이너리
#     `install/.../lib/fairino_hardware_v3_9_7/ros2_cmd_server` 가 살아남아
#     FR5 의 :20005/:8080 을 계속 점유 → real_robot 의 하드웨어 활성화가
#     블로킹 → controller_manager 가 서비스에 응답 못 함 → 스포너 lock 실패.
#     "ServoMoveStart 블로킹이니 전원 재투입" 으로 오진하기 딱 좋다.
#     → 이 스크립트는 **바이너리 경로로 직접 잡고**, `ss` 로 소켓 해제를 확인한다.
#
#  2) 2026-08-17 ─ 런치 패키지명
#     real_robot.launch.py 는 fairino_hardware_v3_9_7 이 아니라
#     **fairino5_v6_moveit2_config** 소속이다.
#
#  3) 2026-08-10 ─ 개통 순서 위반 = 오버런
#     Mode(0)/RobotEnable(1) 없이 real_robot 을 띄우면 write 가 1초씩 밀린다.
#     반드시 cmd_server 에서 ActGripper → Mode → RobotEnable 을 먼저.
#
#  4) `kill -9` 금지. ServoMoveStart 가 블로킹되면 전원 재투입 외엔 답이 없다.
#
#  5) 2026-08-14 ─ fr5-wired 가 자동으로 안 올라와 있을 수 있다(링크 UP·IP 없음).
# ─────────────────────────────────────────────────────────────────────

set -u

FR5_IP=192.168.58.2
LOCAL_IP=192.168.58.10
NIC=enp3s0
CON=fr5-wired
WS=$HOME/fr5_jazzy_test_ws
CMD_BIN="$WS/install/fairino_hardware_v3_9_7/lib/fairino_hardware_v3_9_7/ros2_cmd_server"
LAUNCH_PKG=fairino5_v6_moveit2_config     # ★ hardware 패키지 아님
LOG_DIR=$HOME/fr5_data/logs
PGID_FILE=$HOME/fr5_data/.real_robot_pgid
TWIN_PY=$HOME/fr5_gripper_twin.py             # 실물 그리퍼 → RViz 트윈 동기화
TWIN_PID_FILE=$HOME/fr5_data/.gripper_twin_pid

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; OFF=$'\e[0m'
ok()   { echo "  ${GRN}✔${OFF} $*"; }
warn() { echo "  ${YEL}⚠${OFF} $*"; }
bad()  { echo "  ${RED}✘${OFF} $*"; }
step() { echo; echo "── $*"; }

load_env() {
    export ROS_DOMAIN_ID=73                       # 계약 v0.2.1 §3.3 (3파트 합의)
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export CYCLONEDDS_URI=file://$HOME/cyclonedds_team.xml
    set +u                      # ROS setup.bash 가 미정의 변수를 참조한다
    source /opt/ros/jazzy/setup.bash
    source $WS/install/setup.bash
    set -u
}

# FR5 로 향한 SDK 소켓을 잡고 있는 프로세스 (chrome=WebApp 은 정상이라 제외)
sdk_holders() {
    ss -tnp 2>/dev/null | grep "$FR5_IP" | grep -v chrome
}

cmd_server_pids() { pgrep -f "$CMD_BIN" 2>/dev/null; }

# ═══════════════════════════════ status ═══════════════════════════════
do_status() {
    load_env
    step "네트워크"
    if ip -4 addr show "$NIC" 2>/dev/null | grep -q "$LOCAL_IP"; then
        ok "$NIC = $LOCAL_IP"
    else
        bad "$NIC 에 $LOCAL_IP 없음  →  nmcli con up $CON"
    fi
    ping -c1 -W2 "$FR5_IP" >/dev/null 2>&1 && ok "FR5 ping OK" || bad "FR5 ping 실패"

    step "FR5 SDK 소켓 점유"
    if [ -n "$(sdk_holders)" ]; then sdk_holders | sed 's/^/  /'; else ok "점유 없음"; fi

    step "ROS 노드 (도메인 73)"
    timeout 6 ros2 node list 2>/dev/null | sed 's/^/  /' || warn "노드 없음"

    step "컨트롤러"
    timeout 8 ros2 control list_controllers 2>/dev/null | sed 's/^/  /' \
        || warn "controller_manager 응답 없음"

    step "그리퍼 트윈 동기화"
    local tp; tp=$(pgrep -f "python3 $TWIN_PY" 2>/dev/null)
    if [ -z "$tp" ]; then
        ok "꺼져 있음 (정상) — 팔 동작 가능. RViz 그리퍼만 실물과 따로 논다"
    else
        warn "동작 중 (pid $(echo "$tp" | tr '\n' ' '))"
        echo "     🔴이 상태로는 팔이 움직이지 않는다(SDK 뮤텍스 경합, 2026-08-19)"
        # ★pkill -f 를 안내하지 않는 이유: 그 명령을 담은 셸 자신이 패턴에 걸려
        #   같이 죽는다(자기매칭 — 2026-07 이후 네 번째로 당함). 대괄호 트릭도
        #   같은 명령줄 다른 곳에 스크립트 이름이 있으면 뚫린다. --stop 이 안전하다.
        echo '     끄기: python3 ~/fr5_gripper_twin.py --stop'
        [ "$(echo "$tp" | wc -l)" -gt 1 ] && bad "중복 기동 $(echo "$tp" | wc -l)개 — 경합이 배가된다"
    fi
}

# ════════════════════════════════ down ════════════════════════════════
do_down() {
    load_env
    step "그리퍼 트윈 종료"
    local tp; tp=$(pgrep -f "python3 $TWIN_PY" 2>/dev/null)
    if [ -n "$tp" ]; then
        for p in $tp; do kill -INT "$p" 2>/dev/null; done
        sleep 2
        [ -z "$(pgrep -f "python3 $TWIN_PY" 2>/dev/null)" ] && ok "종료됨" || warn "잔류 — ps 확인"
    else
        ok "실행 중 아님"
    fi
    rm -f "$TWIN_PID_FILE"

    step "런치 종료 (프로세스그룹 단위)"
    if [ -f "$PGID_FILE" ]; then
        local g; g=$(cat "$PGID_FILE")
        kill -INT -"$g" 2>/dev/null && ok "그룹 $g 에 SIGINT" || warn "그룹 $g 없음"
        sleep 6
        kill -TERM -"$g" 2>/dev/null            # ★-9 는 쓰지 않는다
        sleep 3
        rm -f "$PGID_FILE"
    else
        warn "PGID 파일 없음 — 개별 종료 시도"
        pkill -INT -f "real_robot.launch.py" 2>/dev/null
        sleep 6
    fi
    # 잔류 검증 (fr5 스택 바이너리만 — ZK 노드는 건드리지 않는다)
    # ★2026-08-19: 그룹 SIGINT/TERM 을 흘려보내는 ros2_control_node 가 두 번 남았다
    #   (알람 상태에서 SDK 호출에 물려 futex 대기 중이면 그룹 신호를 못 받는다).
    #   남은 pid 에 개별 SIGTERM 을 보내면 1초 만에 죽는다 — kill -9 는 여전히 금지.
    local pat="moveit_ros_move_group/move_group|lib/rviz2/rviz2|controller_manager/ros2_control_node"
    local left
    left=$(pgrep -f "$pat" 2>/dev/null)
    if [ -n "$left" ]; then
        warn "잔류 $(echo "$left" | wc -l) 개 — 개별 SIGTERM"
        for p in $left; do kill -TERM "$p" 2>/dev/null; done
        sleep 3
        left=$(pgrep -f "$pat" 2>/dev/null)
    fi
    [ -z "$left" ] && ok "스택 노드 정리됨" \
        || { bad "잔류 $(echo "$left" | wc -l) 개 — ps 로 확인 필요"; echo "$left" | sed 's/^/    pid /'; }

    step "cmd_server 실 바이너리 종료 (★래퍼 아님)"
    local pids; pids=$(cmd_server_pids)
    if [ -n "$pids" ]; then
        for p in $pids; do kill -INT "$p" 2>/dev/null; done
        sleep 4
        [ -z "$(cmd_server_pids)" ] && ok "종료됨" || bad "아직 살아있음 (kill -9 금지 — 전원 재투입 검토)"
    else
        ok "실행 중 아님"
    fi

    step "SDK 소켓 해제 확인"
    if [ -z "$(sdk_holders)" ]; then ok "전부 해제"; else bad "잔류:"; sdk_holders | sed 's/^/    /'; fi
}

# ═════════════════════════════════ up ═════════════════════════════════
do_up() {
    load_env
    mkdir -p "$LOG_DIR"
    local ts; ts=$(date +%m%d_%H%M)

    step "1. 네트워크"
    if ! ip -4 addr show "$NIC" 2>/dev/null | grep -q "$LOCAL_IP"; then
        warn "$LOCAL_IP 없음 → nmcli con up $CON"
        nmcli con up "$CON" >/dev/null 2>&1
        sleep 3
    fi
    ip -4 addr show "$NIC" 2>/dev/null | grep -q "$LOCAL_IP" \
        && ok "$NIC = $LOCAL_IP" || { bad "IP 확보 실패 — 랜선/nmcli 확인"; return 1; }

    ping -c2 -W2 "$FR5_IP" >/dev/null 2>&1 \
        && ok "FR5 ping OK" || { bad "FR5 응답 없음 — 로봇 전원 확인"; return 1; }

    local sp; sp=$(ethtool "$NIC" 2>/dev/null | grep -E 'Speed|Auto-neg' | tr -d '\t' | paste -sd' ')
    echo "     링크: $sp"      # 100Mb/s + autoneg on 이어야 정상 (8/10 교훈)

    step "2. 선행 점유 정리 (★2026-08-17 함정)"
    if [ -n "$(sdk_holders)" ]; then
        warn "FR5 소켓을 잡고 있는 프로세스가 있다:"
        sdk_holders | sed 's/^/    /'
        local pids; pids=$(cmd_server_pids)
        if [ -n "$pids" ]; then
            echo "     → cmd_server 실 바이너리 종료 시도"
            for p in $pids; do kill -INT "$p" 2>/dev/null; done
            sleep 4
        fi
        [ -z "$(sdk_holders)" ] && ok "해제 완료" \
            || { bad "해제 실패 — 이 상태로 real_robot 을 띄우면 반드시 블로킹된다"; sdk_holders | sed 's/^/    /'; return 1; }
    else
        ok "점유 없음"
    fi

    step "3. cmd_server 기동"
    nohup ros2 run fairino_hardware_v3_9_7 ros2_cmd_server \
        > "$LOG_DIR/cmd_server_$ts.log" 2>&1 &
    local i
    for i in $(seq 1 25); do
        grep -q "Robot connected" "$LOG_DIR/cmd_server_$ts.log" 2>/dev/null && break
        sleep 1
    done
    if grep -q "Robot connected" "$LOG_DIR/cmd_server_$ts.log" 2>/dev/null; then
        ok "Robot connected! (${i}s)"
    else
        bad "연결 실패 — $LOG_DIR/cmd_server_$ts.log 확인"; return 1
    fi

    step "4. 개통 순서 (★이 순서를 어기면 write 오버런)"
    local c res fail=0
    for c in "ActGripper(1,1)" "Mode(0)" "RobotEnable(1)"; do
        res=$(timeout 25 ros2 service call /fairino_remote_command_service \
              fairino_msgs/srv/RemoteCmdInterface "{cmd_str: '$c'}" 2>/dev/null \
              | grep -o "cmd_res='[^']*'" | head -1)
        if echo "$res" | grep -q "cmd_res='0'"; then ok "$c → 0"
        else bad "$c → ${res:-무응답}"; fail=1; fi
        sleep 2
    done
    res=$(timeout 20 ros2 service call /fairino_remote_command_service \
          fairino_msgs/srv/RemoteCmdInterface "{cmd_str: 'GetGripperActivateStatus()'}" 2>/dev/null \
          | grep -o "cmd_res='[^']*'" | head -1)
    echo "     그리퍼 상태: ${res:-무응답}   (0,0,1 이 정상)"
    [ "$fail" = "1" ] && warn "개통 명령 실패 있음 — 알람 상태일 수 있다(ResetAllError 검토)"

    step "5. cmd_server 종료 (단일 클라이언트 원칙)"
    local pids; pids=$(cmd_server_pids)
    for p in $pids; do kill -INT "$p" 2>/dev/null; done      # ★래퍼가 아니라 바이너리
    sleep 4
    if [ -z "$(sdk_holders)" ]; then
        ok "SDK 소켓 해제 확인"
    else
        bad "소켓이 안 풀렸다 — real_robot 이 블로킹된다"; sdk_holders | sed 's/^/    /'; return 1
    fi
    pkill -INT -f "bin/ros2 run fairino_hardware_v3_9_7" 2>/dev/null   # 래퍼도 정리

    step "6. real_robot 기동 (패키지 = $LAUNCH_PKG)"
    setsid ros2 launch "$LAUNCH_PKG" real_robot.launch.py \
        > "$LOG_DIR/real_robot_$ts.log" 2>&1 &
    echo $! > "$PGID_FILE"        # setsid 자식 = 새 프로세스그룹 리더
    for i in $(seq 1 45); do
        timeout 3 ros2 topic info /joint_states 2>/dev/null | grep -q "Publisher count: 1" && break
        sleep 1
    done

    step "6b. twin_bridge (계약 §8: /joint_states → /fr5/joint_states 중계 — 서버·Unity 트윈용)"
    # 8/25 서버팀 지적: 스택 재기동 때 relay 를 같이 안 띄워 /fr5/joint_states 무발행.
    # 공식 인터페이스는 계약대로 /fr5/joint_states 로 확정 — 상시 여기서 기동한다.
    pkill -f 'twin_bridge\.p[y]' 2>/dev/null
    nohup python3 "$HOME/twin_bridge.py" > "$LOG_DIR/twin_bridge_$ts.log" 2>&1 &
    ok "twin_bridge 기동 (30Hz, j1~j6 필터, frame_id=base_link)"

    step "7. 검증"
    timeout 3 ros2 topic info /joint_states 2>/dev/null | grep -q "Publisher count: 1" \
        && ok "/joint_states 발행 중" || { bad "/joint_states 없음 — $LOG_DIR/real_robot_$ts.log 확인"; return 1; }
    timeout 3 ros2 topic info /fr5/joint_states 2>/dev/null | grep -q "Publisher count: 1" \
        && ok "/fr5/joint_states 중계 중 (계약 §8)" || bad "/fr5/joint_states 무발행 — twin_bridge 로그 확인"

    local ctrl; ctrl=$(timeout 12 ros2 control list_controllers 2>/dev/null)
    local n; n=$(echo "$ctrl" | grep -c " active")
    echo "$ctrl" | sed 's/^/     /'
    [ "$n" -ge 3 ] && ok "컨트롤러 ${n}종 active" || bad "active 컨트롤러 ${n}종 (3종이어야 정상)"

    step "8. 그리퍼 트윈 동기화 (기본 꺼짐)"
    # 실물 그리퍼는 MoveGripper 로만 움직이고 ros2_control 쪽은 mock 이라, 이 노드가
    # 없으면 RViz 그리퍼는 실물과 따로 논다. 추종 성능 자체는 좋다(2026-08-19 실측:
    # 개방 8.5s 동안 추종, 최대 지연 1.46mm·정지 후 0.00mm).
    #
    # 🔴그런데 기본을 꺼짐으로 둔다 — 켜면 팔이 못 움직인다 (2026-08-19 실증)
    #   개조 플러그인은 SDK 호출을 _sdk_mutex 로 직렬화한다. 트윈이 5Hz 로
    #   GetGripperCurPosition 을 넣으면 controller_manager 의 실시간 write() 가
    #   굶는다. 실측 write time: 트윈 켜짐 76,000~105,787,943us / 트윈 끔 ~8,000us
    #   (기대 <8,000us). ServoJ 스트림이 깨져 팔이 "성공 보고 + 무동작" 이 된다.
    #   ★증상이 알람과 똑같아서(계획 실패·수동조작 불가) 오진하기 딱 좋다.
    #   폴링을 낮추는 걸로는 부족하다 — 8ms 예산에 76ms 호출 하나면 이미 깨진다.
    #
    #   쓰려면: 팔을 안 움직이는 동안만 손으로 켜고, 팔 움직이기 전에 반드시 끈다.
    #     켜기  FR5_TWIN=1 ~/fr5_up.sh    (또는 python3 ~/fr5_gripper_twin.py &)
    #     끄기  python3 ~/fr5_gripper_twin.py --stop     ~/fr5_up.sh down 도 정리함
    if [ "${FR5_TWIN:-0}" != "1" ]; then
        ok "건너뜀 (기본값) — 팔 동작과 양립 불가. 켜려면 FR5_TWIN=1"
    elif [ ! -f "$TWIN_PY" ]; then
        warn "$TWIN_PY 없음 — 건너뜀"
    else
        setsid python3 "$TWIN_PY" > "$LOG_DIR/gripper_twin_$ts.log" 2>&1 &
        sleep 3
        local tp; tp=$(pgrep -f "python3 $TWIN_PY" | head -1)
        if [ -n "$tp" ]; then
            echo "$tp" > "$TWIN_PID_FILE"
            ok "그리퍼 트윈 동기화 중 (pid $tp, 5Hz)"
            warn "🔴이 상태로는 팔이 움직이지 않는다 — 팔 쓰기 전에 반드시 끌 것"
        else
            warn "트윈 노드 기동 실패 — $LOG_DIR/gripper_twin_$ts.log 확인"
        fi
    fi

    echo
    echo "${GRN}━━ 기동 완료 ━━${OFF}"
    echo "  로그   : $LOG_DIR/{cmd_server,real_robot}_$ts.log"
    echo "  자세   : python3 ~/fr5_pose.py list | show | goto <이름>"
    echo "  ⚠ 티칭 전 백업: cp ~/fr5_data/poses.json ~/fr5_data/poses.json.BEFORE_\$(date +%m%d)"
    echo "  정리   : ~/fr5_up.sh down"
}

case "${1:-up}" in
    up|"")   do_up ;;
    down)    do_down ;;
    status)  do_status ;;
    *)       echo "사용법: $0 [up|down|status]"; exit 1 ;;
esac
