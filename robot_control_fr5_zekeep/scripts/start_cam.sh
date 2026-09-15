#!/usr/bin/env bash
# 손목캠 서버 기동 (8/27) — D435 가 이 PC USB 에 있으면 rs, 없으면 ROS 토픽 자동탐색(비전 PC 가 카메라 들고 있을 때)
#   ./start_cam.sh            # 자동
#   ./start_cam.sh rs|ros|udp # 강제 (udp = 카메라 PC 에서 cam_udp_send.py --to <이 PC IP>, ROS 불필요)
#   ./start_cam.sh            # auto: USB 에 D435 있으면 rs, 없으면 ${NET_MODE:-udp} (udp|ros)
#   DEPTH=auto ./start_cam.sh ros   # 캘리브용 뎁스도 받기(유선일 때만)
set -u
cd "$(dirname "$0")"
MODE=${1:-auto}
if [ "$MODE" = auto ]; then
  # D435 는 파이프 stop 직후 USB 재열거(Device 번호 증가) 중이라 잠깐 안 보일 수 있음 → 최대 8s 재확인
  MODE=${NET_MODE:-udp}
  for i in 1 2 3 4; do
    if lsusb | grep -q '8086:0b07'; then MODE=rs; break; fi; sleep 2
  done
fi
fuser -k 8766/tcp 2>/dev/null; sleep 1
if [ "$MODE" = rs ]; then
  # 9/1: CAM_EXPOSURE=166 = 조명 120Hz 플리커 안전값(다른 노출이면 도트가 35px 씩 흔들림, 실증).
  #      CAM_DET_HZ=8 = 검출 8Hz 분리(CPU 182%→80%).
  # 9/2 실측 임계(파랑 S160=유령 컷·빨강 V55 S160=어두울 때 소실 방지·노랑 S140)
  CAM_DET_HZ=${CAM_DET_HZ:-8} CAM_EXPOSURE=${CAM_EXPOSURE:-166} CAM_GAIN=${CAM_GAIN:-16} CAM_WB=${CAM_WB:-4600} \
  CAM_BLUE_S=${CAM_BLUE_S:-245} CAM_RED_S=${CAM_RED_S:-160} CAM_RED_V=${CAM_RED_V:-45} \
  CAM_YELLOW_S=${CAM_YELLOW_S:-140} setsid -f python3 cam_server.py --source rs > /tmp/cam_server.log 2>&1
elif [ "$MODE" = udp ]; then
  setsid -f python3 cam_server.py --source udp --udp-port ${UDP_PORT:-5005} > /tmp/cam_server.log 2>&1
else
  setsid -f bash -c "export ROS_DOMAIN_ID=${DOMAIN:-73} RMW_IMPLEMENTATION=rmw_cyclonedds_cpp CYCLONEDDS_URI=file://\$HOME/cyclonedds_team.xml
    source /opt/ros/jazzy/setup.bash
    exec python3 cam_server.py --source ros --color-topic ${COLOR:-auto} --depth-topic ${DEPTH:-off}" > /tmp/cam_server.log 2>&1
fi
sleep 5
# ★9/2: 조명(아침 자연광 vs 저녁 실내등)에 따라 고정노출이 과다/과소가 된다.
#   관측자세 실측: 밝기 207(노출166) → 빨강 면적변동 50%·노랑 22%, 밝기 108(노출41) → 2%·1%.
#   → 노출을 박지 말고 '평균 밝기 목표'로 맞춘 뒤 고정한다. WB·PLF 는 이미 고정.
if [ "$MODE" = rs ]; then
  # ★9/8: WB 는 "이미 고정" 이 아니다 — 재기동하면 5500 으로 돌아간다(파랑 점 면적 1823→258 실측).
  curl -s -m 20 "http://127.0.0.1:8766/expo?wb=4600&awb=0" >/dev/null
  curl -s -m 20 "http://127.0.0.1:8766/expo?bright=${CAM_TARGET_BRIGHT:-108}" >/dev/null
  echo "  밝기 정규화 → $(curl -s -m 5 http://127.0.0.1:8766/expo)"
fi
echo "━━ 손목캠 [$MODE] http://$(hostname -I | awk '{print $1}'):8766  로그 /tmp/cam_server.log"
curl -s -m 3 http://127.0.0.1:8766/health; echo
grep -E '^\[(ros|udp)\]' /tmp/cam_server.log | tail -2
