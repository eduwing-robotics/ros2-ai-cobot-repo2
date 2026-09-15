#!/usr/bin/env python3
"""
zk_pickplace.py - 픽앤플레이스 1사이클

  집는곳에서 흡착 → 들어올림 → 옆으로 이동 → 내림 → 놓음 → 빠짐 → 복귀

★ 방향 규약 (2026-08-06 사용자 육안 확정)
    A2·A3 모두 '각도가 0에 가까워질수록 위'. A2가 상승 주역, A3는 보조.
    따라서 들어올림 = 각도 + 방향.

안전
  · 소프트리밋 / X5(급정지) / 통신두절 → 즉시 중단
  · finally 에서 조그 비트 전부 해제 + 펌프·밸브 OFF
  · 물건을 든 구간에서는 반드시 들어올림이 끝난 뒤에만 A1을 돌린다
"""
import json, sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK, PUMP, VALVE
from zk_safety import Guard, Abort, HOME_FLAG, JOINT_LIMITS
import zk_pose as P
import zk_profiles as ZPROF

PICK = "pick1"
LIFT_A2, LIFT_A3 = 25.0, 15.0      # 들어올림 양(도)
PRESS   = 4.0                      # 집을 때 흡착판을 물건에 눌러주는 추가 하강(도)
SUCK_S  = 3.0                      # 진공 형성 대기(초). 1.5초로는 못 들었음(실측)
SLOW    = ((20.0, 2.0), (8.0, 0.6))   # 물건을 든 구간
FASTP   = ((50.0, 4.5), (10.0, 0.8))  # 무부하 구간 — 감속여유는 속도에 비례해 키운다  # 물건을 든 구간의 속도 — 천천히
PLACE_A1 = -120.0                  # 놓는 자리 (베이스 회전)

db = json.load(open(P.POSE_FILE))
pick = db[PICK]
zk = ZK(P.apply_profile(ZPROF.resolve_from_argv()).port)
if not zk.link():
    sys.exit("링크 실패")

def step(n, msg):
    print(f"\n{'='*60}\n[{n}] {msg}\n{'='*60}", flush=True)

def go(j, t, slow=False):
    lo, hi = JOINT_LIMITS[j]
    if not (lo <= t <= hi):
        raise Abort(f"{j} 목표 {t:.1f}도가 소프트리밋 밖")
    P.goto_axis(zk, guard, j, t, 0.5, SLOW if slow else FASTP)

t_start = time.time()
try:
    ms = zk.m(32) or set()
    miss = [j for j, b in HOME_FLAG.items() if b not in ms]
    if miss:
        sys.exit(f"원점 미확보: {miss} — 먼저 zk_home_run.py")
    guard = Guard(zk, homing=False)
    guard.preflight(P.angles(zk))

    up_a2, up_a3 = pick["A2"] + LIFT_A2, pick["A3"] + LIFT_A3
    print(f"집는 자리  A1={pick['A1']:.2f} A2={pick['A2']:.2f} A3={pick['A3']:.2f}")
    print(f"들린 자세  A2={up_a2:.2f}  A3={up_a3:.2f}   놓는 자리 A1={PLACE_A1:.2f}")

    step(1, "집는 자리로 이동")
    for j in ("A1", "A2", "A3"):
        go(j, pick[j])

    step(2, f"흡착판 눌러주기 (A2를 {PRESS}도 더 하강)")
    go("A2", pick["A2"] - PRESS, slow=True)

    step(3, f"흡착 ON — 펌프 Y14, {SUCK_S}초 진공 형성")
    zk.y_off(VALVE); time.sleep(0.2)
    zk.y_on(PUMP)
    time.sleep(SUCK_S)
    ys = zk.y() or set()
    print(f"  펌프 {'✅ ON' if PUMP in ys else '🔴 OFF'}  ({SUCK_S}초 흡입)", flush=True)

    step(4, "물건 들어올림 — 천천히 (A2 위로, A3 위로)")
    go("A2", up_a2, slow=True); go("A3", up_a3, slow=True)
    print("\n  ▶▶ 3초 정지 — 물건이 붙어 있는지 봐주세요", flush=True)
    time.sleep(1.5)

    step(5, f"옆으로 이동  A1 {pick['A1']:.1f}도 → {PLACE_A1:.1f}도")
    go("A1", PLACE_A1, slow=True)

    step(6, "내려놓기 위해 하강")
    go("A3", pick["A3"], slow=True); go("A2", pick["A2"], slow=True)

    step(7, "흡착 해제 — 펌프 OFF + 밸브 1초 배기")
    zk.y_off(PUMP); time.sleep(0.3)
    zk.y_on(VALVE); time.sleep(1.0); zk.y_off(VALVE)
    time.sleep(0.5)
    print("  해제 완료", flush=True)

    step(8, "물건에서 빠짐 (다시 들어올림)")
    go("A2", up_a2); go("A3", up_a3)

    step(9, "집는 자리로 복귀")
    go("A1", pick["A1"])
    go("A3", pick["A3"]); go("A2", pick["A2"])

    a = P.angles(zk)
    print(f"\n{'='*60}")
    print(f"★ 사이클 완료  {time.time()-t_start:.1f}초")
    print(f"  최종 {P.fmt(a)}")
    print("  집는자리 대비 오차  " + "  ".join(
        f"{j}={a[j]-pick[j]:+.2f}도" for j in P.ORDER))
    print('='*60)
except Abort as e:
    print(f"\n■ 안전 중단: {e}", flush=True)
finally:
    P.all_off(zk)
    zk.y_off(PUMP); zk.y_off(VALVE)
    ys = zk.y() or set(); ms = zk.m(32) or set()
    print(f"\n정리: 조그비트 {[b for b in P.ALL_JOG if b in ms] or '✅ 없음'}"
          f"   펌프/밸브 {'🔴 남음' if (PUMP in ys or VALVE in ys) else '✅ OFF'}")
    print(zk.report())
    zk.close()
