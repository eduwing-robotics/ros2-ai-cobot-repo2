#!/usr/bin/env bash
# team_env.sh — 팀 ROS2 망(M1 합의: 도메인 73 + 유니캐스트) 환경 전환
#
#   source ~/team_env.sh            # 팀망 (서버·비전과 통신)
#   source ~/team_env.sh --local    # 되돌리기 (기존 격리 운용 97 + unicast.xml)
#
# ★왜 스크립트인가: .bashrc 는 ROS_DOMAIN_ID=97 로 박혀 있다(로컬 실기 운용).
#   서버 개통은 도메인 73 이므로, 터미널마다 명시적으로 전환한다.
#   ★전환한 터미널에서 띄운 노드만 팀망에 보인다 — 실기 스택(zk 노드·FR5·
#   오케스트레이터) 전부 같은 도메인에서 띄워야 서버가 구독할 수 있다.
#
# ⚠FR5 제어망(enp3s0, 192.168.58.x)은 cyclonedds_team.xml 에서 제외돼 있다.
#   로봇 제어 트래픽 보호 — 이 원칙을 깨지 말 것(M1 §1).

if [ "$1" = "--local" ]; then
    export ROS_DOMAIN_ID=97
    export CYCLONEDDS_URI=file://$HOME/cyclonedds_unicast.xml
    echo "[로컬 격리] ROS_DOMAIN_ID=97  URI=cyclonedds_unicast.xml"
else
    export ROS_DOMAIN_ID=73
    export CYCLONEDDS_URI=file://$HOME/cyclonedds_team.xml
    unset ROS_AUTOMATIC_DISCOVERY_RANGE
    echo "[팀망] ROS_DOMAIN_ID=73  URI=cyclonedds_team.xml"
fi
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

# 팀망 NIC 실측 확인 (xml 의 NetworkInterface 와 일치해야 discovery 가 열린다)
_nic=$(grep -oP 'NetworkInterface name="\K[^"]+' "$HOME/cyclonedds_team.xml" 2>/dev/null | grep -v '^lo$' | head -1)
_ip=$(ip -4 -o addr show "$_nic" 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
if [ -z "$_ip" ]; then
    echo "  ⚠ xml NIC '$_nic' 에 IP 없음 — 팀 공유기 연결 확인 (유선 전환 시 xml 한 줄 교체)"
else
    echo "  NIC $_nic = $_ip"
    [ "$_ip" != "192.168.20.10" ] && \
        echo "  ⚠ M1 합의 예약 IP 는 192.168.20.10 — 현재 $_ip 라면 서버/비전 xml 의 Peer 와 어긋난다(공유기 IP 예약 필요)"
fi
unset _nic _ip
