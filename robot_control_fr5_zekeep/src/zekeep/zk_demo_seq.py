#!/usr/bin/env python3
"""자세 시퀀스 시연 + 반복정밀도 측정"""
import json, sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK
from zk_safety import Guard, Abort, HOME_FLAG
import zk_pose as P
import zk_profiles as ZPROF

SEQ = sys.argv[1:] or ["pick1", "wide", "mid", "pick1"]
db = json.load(open(P.POSE_FILE))
zk = ZK(P.apply_profile(ZPROF.resolve_from_argv()).port)
if not zk.link():
    sys.exit("링크 실패")
try:
    ms = zk.m(32) or set()
    miss = [j for j, b in HOME_FLAG.items() if b not in ms]
    if miss:
        sys.exit(f"원점 미확보: {miss}")
    guard = Guard(zk, homing=False)
    guard.preflight(P.angles(zk))
    hist = []
    for i, name in enumerate(SEQ, 1):
        tgt = db[name]
        print(f"\n{'='*62}\n[{i}/{len(SEQ)}]  → {name}   "
              + "  ".join(f"{j}={tgt[j]:.2f}" for j in P.ORDER)
              + f"\n{'='*62}", flush=True)
        t0 = time.time()
        for j in P.ORDER:
            P.goto_axis(zk, guard, j, tgt[j], 0.5)
        a = P.angles(zk)
        err = {j: a[j] - tgt[j] for j in P.ORDER}
        hist.append((name, a, err))
        print(f"  ⏱ {time.time()-t0:.1f}초   오차  "
              + "  ".join(f"{j}={err[j]:+.2f}°" for j in P.ORDER), flush=True)
    print(f"\n{'='*62}\n요약\n{'='*62}")
    for name, a, err in hist:
        print(f"  {name:<8} " + "  ".join(f"{j}={a[j]:8.2f}°" for j in P.ORDER)
              + "   오차 " + " ".join(f"{err[j]:+.2f}" for j in P.ORDER))
    p1 = [h for h in hist if h[0] == "pick1"]
    if len(p1) >= 2:
        print("\n  ★ pick1 반복정밀도 (1회차 vs 마지막):")
        for j in P.ORDER:
            d = p1[-1][1][j] - p1[0][1][j]
            print(f"     {j}  {p1[0][1][j]:8.2f}° → {p1[-1][1][j]:8.2f}°   "
                  f"차이 {d:+.3f}°  ({d*177.78:+.0f} 펄스)")
except Abort as e:
    print(f"\n■ 안전 중단: {e}")
finally:
    P.all_off(zk)
    ms = zk.m(32) or set()
    st = [b for b in P.ALL_JOG if b in ms]
    print(f"\n조그 비트 잔류: {st if st else '✅ 없음'}")
    print(zk.report())
    zk.close()
