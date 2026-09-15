#!/usr/bin/env python3
"""
zk_jog_log.py - 조그 관찰 기록기 (읽기 전용, 쓰기 없음)

백그라운드로 돌려놓고 사람이 HMI로 조그하는 동안 관절각·입력·출력을 기록한다.
(대화 중 감시는 안내를 읽기 전에 창이 닫혀버려서, 기록은 반드시 백그라운드로 돈다.)

판별
  · 각도가 변하는데 팔이 안 움직임 → 펄스는 나감, 모터가 못 따라감 = 탈조/전기/기구
  · 각도가 아예 안 변함           → 래더가 펄스를 안 냄 = 조그 비트/인터록 문제

    python3 zk_jog_log.py [기록초]
"""
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D, A_X, A_Y  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 180
JOINTS = (("A1", 1010), ("A2", 1030), ("A3", 1050))
XN = {0: "X0(A1리밋)", 1: "X1(A2리밋)", 2: "X2(A3리밋)",
      3: "X3시작", 4: "X4원점", 5: "X5급정지", 6: "X6적외선"}
YN = {0: "A1펄스", 1: "A2펄스", 2: "A3펄스", 4: "A1방향", 5: "A2방향",
      6: "A3방향", 8: "A1_ENA", 9: "A2_ENA", 10: "A3_ENA", 12: "펌프", 13: "밸브"}


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


zk = ZK(ZPROF.resolve_from_argv().port)
if not zk.link():
    sys.exit("링크 실패")

print(f"기록 시작 ({DUR}초) — 지금 조그하세요", flush=True)
base = {n: ang(zk, d) for n, d in JOINTS}
print(f"  시작 각도  " + "  ".join(
    f"{n}={'----' if base[n] is None else f'{base[n]:.2f}'}" for n, _ in JOINTS), flush=True)

peak = {n: 0.0 for n, _ in JOINTS}
prev = None
t0 = time.time()
while time.time() - t0 < DUR:
    a = {n: ang(zk, d) for n, d in JOINTS}
    xb = zk.read(A_X, 1)
    yb = zk.read(A_Y, 2)
    xs = set() if xb is None else {i for i in range(7) if xb[0] >> i & 1}
    ys = set() if yb is None else {i for i in range(14) if yb[i // 8] >> (i % 8) & 1}

    for n, _ in JOINTS:
        if a[n] is not None and base[n] is not None:
            peak[n] = max(peak[n], abs(a[n] - base[n]))

    cells = "  ".join(f"{n}={'   ----' if a[n] is None else f'{a[n]:8.2f}'}"
                      for n, _ in JOINTS)
    xin = ",".join(XN[i] for i in sorted(xs)) or "-"
    yout = ",".join(YN.get(i, str(i)) for i in sorted(ys)) or "-"
    line = f"{cells}  X[{xin}]  Y[{yout}]"
    if line != prev:
        print(f"  {time.time()-t0:6.1f}  {line}", flush=True)
        prev = line
    time.sleep(0.1)

print("\n── 집계: 시작점 대비 최대 이동량 ──", flush=True)
for n, _ in JOINTS:
    v = peak[n]
    tag = "움직임 없음 (펄스가 안 나감)" if v < 0.05 else f"{v:.2f}° 움직임"
    print(f"   {n}  {tag}", flush=True)
print(f"\n{zk.report()}", flush=True)
zk.close()
