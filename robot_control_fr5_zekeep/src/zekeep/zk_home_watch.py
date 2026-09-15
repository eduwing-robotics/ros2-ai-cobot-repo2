#!/usr/bin/env python3
"""
zk_home_watch.py - 원점복귀 실시간 감시 (읽기 전용, 쓰기 없음)

성공 판정 = M121/M122/M123 (A1/A2/A3 원점에 있음) 이 켜지는 것.
리밋 스위치가 물렸다 풀리는 순서와 각도 변화도 같이 찍는다.

    python3 zk_home_watch.py [감시초]
"""
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 60
JOINTS = (("A1", 1010), ("A2", 1030), ("A3", 1050))
FLAGS = ((120, "원점복귀중"), (121, "A1원점"), (122, "A2원점"), (123, "A3원점"))


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


zk = ZK(ZPROF.resolve_from_argv().port)
if not zk.link():
    sys.exit("링크 실패")

print(f"감시 {DUR}초 시작 — 지금 HMI에서 원점복귀를 누르세요.", flush=True)
print(f"{'경과':>6} {'A1':>10} {'A2':>10} {'A3':>10}  {'리밋':<12} {'플래그'}", flush=True)

t0 = time.time()
prev_line = None
done_announced = False
while time.time() - t0 < DUR:
    xs = zk.x() or set()
    ms = zk.m(32) or set()
    a = [ang(zk, d) for _, d in JOINTS]

    lim = ",".join(n for i, n in ((0, "X0"), (1, "X1"), (2, "X2")) if i in xs) or "-"
    fl = ",".join(n for b, n in FLAGS if b in ms) or "-"
    cells = " ".join("     ----" if v is None else f"{v:10.3f}" for v in a)
    line = f"{cells}  {lim:<12} {fl}"

    if line != prev_line:
        print(f"{time.time()-t0:6.1f} {line}", flush=True)
        prev_line = line

    if {121, 122, 123} <= ms and not done_announced:
        print("\n  ★★ 원점복귀 성공 — 세 축 모두 원점 플래그 ON. 카운터 재동기 완료.",
              flush=True)
        done_announced = True

    time.sleep(0.15)

xs, ms = zk.x() or set(), zk.m(32) or set()
print("\n── 최종 ──", flush=True)
for _, d in JOINTS:
    pass
print(f"  각도  A1={ang(zk,1010)}  A2={ang(zk,1030)}  A3={ang(zk,1050)}", flush=True)
print(f"  원점플래그  M121={121 in ms}  M122={122 in ms}  M123={123 in ms}", flush=True)
print(f"  리밋  X0={0 in xs} X1={1 in xs} X2={2 in xs}", flush=True)
print(f"\n{zk.report()}", flush=True)
zk.close()
