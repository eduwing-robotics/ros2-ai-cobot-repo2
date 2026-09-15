#!/usr/bin/env python3
"""방향 확인 시연 — 예고된 순서대로 천천히, 한 축씩"""
import sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK
from zk_safety import Guard
import zk_pose as P
import zk_profiles as ZPROF

zk = ZK(P.apply_profile(ZPROF.resolve_from_argv()).port)
if not zk.link():
    sys.exit("링크 실패")
g = Guard(zk, homing=False)
a0 = P.angles(zk)
g.preflight(a0)
print(f"시작 자세 {P.fmt(a0)}", flush=True)
print("10초 뒤 시작합니다 — 팔 끝을 보세요", flush=True)
try:
    time.sleep(10)

    print("\n[1] ▶▶ A2 이동 시작  %.1f° → %.1f°" % (a0['A2'], a0['A2']+20), flush=True)
    P.goto_axis(zk, g, "A2", a0['A2'] + 20, 0.5)
    print("[1] ■ A2 정지 — 5초 유지. 팔이 위로 갔나요 아래로 갔나요?", flush=True)
    time.sleep(5)

    print("\n[2] ◀◀ A2 원위치 복귀", flush=True)
    P.goto_axis(zk, g, "A2", a0['A2'], 0.5)
    print("[2] ■ A2 복귀 완료 — 8초 대기", flush=True)
    time.sleep(8)

    print("\n[3] ▶▶ A3 이동 시작  %.1f° → %.1f°" % (a0['A3'], a0['A3']+25), flush=True)
    P.goto_axis(zk, g, "A3", a0['A3'] + 25, 0.5)
    print("[3] ■ A3 정지 — 5초 유지. 이번엔 어느 쪽인가요?", flush=True)
    time.sleep(5)

    print("\n[4] ◀◀ A3 원위치 복귀", flush=True)
    P.goto_axis(zk, g, "A3", a0['A3'], 0.5)
    print("[4] ■ 전부 복귀 완료", flush=True)
finally:
    P.all_off(zk)
    print(f"\n최종 {P.fmt(P.angles(zk))}", flush=True)
    ms = zk.m(32) or set()
    print("비트 잔류:", [b for b in P.ALL_JOG if b in ms] or "✅ 없음", flush=True)
    print(zk.report(), flush=True)
    zk.close()
