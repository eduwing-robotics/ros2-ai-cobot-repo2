#!/usr/bin/env python3
"""HMI가 '시작'할 때 무엇을 바꾸는지 기록 (읽기 전용)

M비트 전체(M0~M255)와 주요 D를 계속 스냅샷 떠서 변화만 출력한다.
사람이 HMI(또는 물리 START 버튼)로 자동운전을 시작하는 동안 돌려놓으면,
우리가 PC에서 빠뜨린 조건이 그대로 드러난다.
"""
import sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK
import zk_profiles as ZPROF

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 180
DREG = [10, 20, 30, 40, 50, 60, 70, 76, 78, 90, 2002, 2004, 2024]

zk = ZK(ZPROF.resolve_from_argv().port)
if not zk.link():
    sys.exit("링크 실패")

print(f"기록 시작 ({DUR}초) — 지금 HMI에서 자동운전을 시작하세요", flush=True)
pm = zk.m(32) or set()
pd = {d: zk.d(d) for d in DREG}
px = zk.x() or set()
py = zk.y() or set()
print(f"  기준 M: {sorted(pm)}", flush=True)
print(f"  기준 D: {pd}", flush=True)

t0 = time.time()
while time.time() - t0 < DUR:
    ms = zk.m(32)
    if ms is None:
        continue
    xs = zk.x() or set()
    ys = zk.y() or set()
    t = time.time() - t0

    on, off = sorted(ms - pm), sorted(pm - ms)
    if on or off:
        print(f"  {t:6.1f}  M  +{on}  -{off}", flush=True)
        pm = ms
    if xs != px:
        print(f"  {t:6.1f}  X  {sorted(xs)}", flush=True)
        px = xs
    if ys != py:
        print(f"  {t:6.1f}  Y  {sorted(ys)}", flush=True)
        py = ys
    for d in DREG:
        v = zk.d(d)
        if v is not None and v != pd[d]:
            print(f"  {t:6.1f}  D{d}: {pd[d]} → {v}", flush=True)
            pd[d] = v

print("── 기록 종료 ──", flush=True)
print(zk.report(), flush=True)
zk.close()
