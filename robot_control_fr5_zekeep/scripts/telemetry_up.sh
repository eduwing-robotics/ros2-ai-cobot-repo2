#!/usr/bin/env bash
# telemetry_up.sh — 계약 §8 텔레메트리 송신기 기동 (FMS 서버 · Unity 트윈용)
#
#   ~/telemetry_up.sh --check    # 수동적 사전점검만 (노드 기동 없음, 데이터 0건)
#   ~/telemetry_up.sh            # 팀망(도메인 73)에서 twin_bridge 기동 = FR5 송신 시작
#
# ★§8 송신기는 이 스크립트 하나가 아니라 "팀망 도메인에서 뜬 노드 4개"다:
#   /fr5/joint_states     ← twin_bridge.py       (이 스크립트가 기동)
#   /zkbot1/joint_states  ← zk_ros2_node.py robot:=zkbot1  (사용자가 별도 기동)
#   /zkbot2/joint_states  ← zk_ros2_node.py robot:=zkbot2  (사용자가 별도 기동)
#   /cell/status 1Hz      ← cell_orchestrator.py           (사용자가 별도 기동)
#   → 전부 `source ~/team_env.sh` 한 터미널에서 띄워야 서버(.20.20)가 구독한다.
#   ⚠8/13 zk_ros2_node 에 frame_id 패치 반영 — zk 노드는 **재기동해야** 적용된다.
#
# ⚠전송은 ROS2 토픽(CycloneDDS 유니캐스트)이다. HTTP/WebSocket 엔드포인트 없음.
#   DDS 는 UDP 라 nc -z 식 포트 점검이 안 된다 — 대신 ping + NIC 정합만 본다.
#   (참고: 도메인 73 디스커버리 포트 = 7400 + 250*73 = UDP 25650대)

set -u
SERVER_IP=192.168.20.20     # FMS 서버 PC (M1 합의, IP 예약)
VISION_IP=192.168.20.30     # 비전 PC (참고용)
MY_IP_EXPECT=192.168.20.10  # 로봇팔 PC 예약 IP
XML="$HOME/cyclonedds_team.xml"

echo "== §8 텔레메트리 사전점검 =="

# 1) 팀망 NIC — team_env.sh 와 같은 방법으로 xml 에서 읽는다 (단일 출처)
NIC=$(grep -oP 'NetworkInterface name="\K[^"]+' "$XML" 2>/dev/null | grep -v '^lo$' | head -1)
IP=$(ip -4 -o addr show "$NIC" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
if [ -z "${IP:-}" ]; then
    echo "✗ NIC '$NIC' 에 IP 없음 — 팀 공유기 연결부터 (유선 전환했으면 xml 의 NIC 한 줄 교체)"
    NET_OK=0
else
    echo "✓ NIC $NIC = $IP"
    [ "$IP" != "$MY_IP_EXPECT" ] && \
        echo "  ⚠ 예약 IP 는 $MY_IP_EXPECT — 서버/비전 xml 의 Peer 와 어긋난다 (공유기 IP 예약 확인)"
    NET_OK=1
fi

# 2) 서버·비전 ping (수동적 점검 — 데이터 송신 아님)
for tgt in "$SERVER_IP:FMS서버" "$VISION_IP:비전"; do
    ip_=${tgt%%:*}; name=${tgt##*:}
    if ping -c 2 -W 1 "$ip_" >/dev/null 2>&1; then
        echo "✓ $name $ip_ ping 응답"
    else
        echo "✗ $name $ip_ ping 무응답 — $([ "$name" = FMS서버 ] && echo '개통 불가, 서버 PC 접속/IP 예약 확인' || echo '비전은 §8 필수 아님, 참고만')"
    fi
done

# 3) FR5 제어망 보호 원칙 (M1 §1) — 팀 xml 에 enp3s0 이 섞였는지 검사
if grep -q 'name="enp3s0"' "$XML" 2>/dev/null; then
    echo "✗ cyclonedds_team.xml 에 enp3s0(FR5 제어망)이 들어있다 — 즉시 제거할 것"
else
    echo "✓ FR5 제어망(enp3s0) 팀 xml 에서 배제됨"
fi

# 4) zk 노드 frame_id 패치 반영 여부 (재기동 필요 알림)
if grep -q 'FRAME_IDS' "$HOME/zk_ros2_node.py"; then
    echo "✓ zk_ros2_node frame_id 패치(0813) 존재 — 이미 떠 있는 zk 노드는 재기동해야 반영"
fi

if [ "${1:-}" = "--check" ]; then
    echo "== 점검만 수행 (노드 기동 안 함) =="
    exit 0
fi

[ "${NET_OK:-0}" = 1 ] || { echo "네트워크 미비 — 기동 중단"; exit 1; }

# ---- 기동: 팀망 도메인에서 twin_bridge (FR5 §8 중계) ----
# 원본 /joint_states 는 FR5 스택(real_robot)이 같은 도메인에 떠 있어야 들어온다.
source /opt/ros/jazzy/setup.bash
source "$HOME/team_env.sh"
echo "== twin_bridge 기동 (FR5 /joint_states → /fr5/joint_states, 30Hz) =="
exec python3 "$HOME/twin_bridge.py" "$@"
