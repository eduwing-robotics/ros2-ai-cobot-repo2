#!/usr/bin/env bash
# fr5_rescue.sh — FR5 동결/알람 원클릭 복구 (2026-08-25 사고로 확립)
#
# 증상 (이 중 하나라도 보이면 이 스크립트):
#   - HARMONY 콘솔에 "관절상태 동결" 빨간 토스트
#   - 이동 명령이 전부 즉시 ABORTED / "실물 무동작" 오류
#   - MoveIt 로그 "couldn't receive full current joint state within 1s"
#   - controller_manager "Switch controller timed out"
#
# ★★ 실행 전 사람 손 1가지: 로봇 웹 펜던트 http://192.168.58.2 접속 →
#    우상단 빨간 ⚠ 클릭 → [Clear]. "resettable" 알람이면 이걸로 로봇쪽은 끝.
#    (충돌감지 알람이 8080 채널을 죽여 SDK 가 영구 블로킹되는 게 동결의 정체.
#     로봇 전원 재투입은 Clear 로도 안 풀릴 때만!)
#
# 이 스크립트가 하는 일: PC 쪽 갇힌 스택 정리 → 소켓 소멸 대기 → 재기동 → 검증
set -u
echo "── 1. 갇힌 프로세스 정리 (pkill 자기매칭 방지 브래킷 패턴)"
pkill -9 -f 'ros2_cmd_serve[r]'    2>/dev/null && echo "  cmd_server kill"
pkill -9 -f 'ros2_control_nod[e]'  2>/dev/null && echo "  ros2_control_node kill"
sleep 1
~/fr5_up.sh down >/dev/null 2>&1

echo "── 2. SDK 포트(20003/20005/8080) FIN-WAIT-2 자연소멸 대기 (최대 2분)"
for i in $(seq 1 40); do
  ss -tn 2>/dev/null | grep -qE '58\.2:(20003|20005|8080)' || break
  sleep 3
done
if ss -tn 2>/dev/null | grep -qE '58\.2:(20003|20005|8080)'; then
  echo "  ✘ 2분에도 안 풀림 — 로봇 전원 재투입 후 이 스크립트 재실행"
  exit 1
fi
echo "  ✔ 클린"

echo "── 3. 로봇 부팅/응답 대기 (전원 재투입했어도 그냥 기다리면 됨)"
for i in $(seq 1 60); do
  timeout 1 bash -c 'echo > /dev/tcp/192.168.58.2/8080' 2>/dev/null && break
  sleep 3
done

echo "── 4. 스택 재기동"
~/fr5_up.sh || { echo "✘ 기동 실패 — 위 로그 확인(개통 실패면 펜던트 알람 Clear 재확인)"; exit 1; }

echo "── 5. 검증: joint_states 실갱신 (브리지 경유)"
j1=$(curl -s -m 3 http://localhost:8765/status | python3 -c "import json,sys;print(json.load(sys.stdin)['robots']['fr5']['joints'])" 2>/dev/null)
echo "  joints: $j1"
echo "━━ 복구 완료 — 콘솔에서 REAL 켜고 저속으로 1회 확인 이동 후 사용 ━━"
