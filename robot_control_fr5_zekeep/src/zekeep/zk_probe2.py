#!/usr/bin/env python3
"""티칭 배열 정렬 확인 + 원점 플래그 확인 (읽기 전용)"""
import sys
sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D
import zk_profiles as ZPROF

SCALE = 177.78  # 펄스/도 — D1000↔D1010, D1020↔D1030, D1040↔D1050 3축 교차검증값


def words(zk, start, n):
    out = []
    got = 0
    while got < n:
        c = min(32, n - got)
        b = zk.read(A_D + (start + got) * 2, c * 2)
        out.extend([None] * c if b is None else
                   [int.from_bytes(b[i * 2:i * 2 + 2], "little") for i in range(c)])
        got += c
    return out


def i32(lo, hi):
    if lo is None or hi is None:
        return None
    v = lo | (hi << 16)
    return v - (1 << 32) if v & 0x80000000 else v


zk = ZK(ZPROF.resolve_from_argv().port)
if not zk.link():
    sys.exit("링크 실패")

print("=== 원시 워드 (16진) — 정렬 확인용 ===")
for base, nm in ((100, "X/A1"), (200, "Y/A2"), (300, "Z/A3")):
    w = words(zk, base, 24)
    print(f"\nD{base}~D{base+23}  [{nm}]")
    for r in range(0, 24, 8):
        print(f"  D{base+r:<4} " + " ".join(
            "----" if v is None else f"{v:04X}" for v in w[r:r + 8]))

print("\n=== 32bit 해석 (스텝당 2워드) ===")
wx, wy, wz = words(zk, 100, 24), words(zk, 200, 24), words(zk, 300, 24)
print(f"{'스텝':>4} {'A1펄스':>12}{'A1도':>10} {'A2펄스':>12}{'A2도':>10} "
      f"{'A3펄스':>12}{'A3도':>10}")
for s in range(12):
    o = s * 2
    vs = [i32(w[o], w[o + 1]) for w in (wx, wy, wz)]
    cells = []
    for v in vs:
        cells.append(f"{str(v):>12}" + (f"{v/SCALE:9.1f}°" if v is not None else "        -"))
    print(f"{s:>4} " + " ".join(cells))

print("\n=== 원점/완료 플래그 ===")
ms = zk.m(32) or set()
for n, nm in ((120, "원점복귀 실행"), (121, "A1 원점에 있음"), (122, "A2 원점에 있음"),
              (123, "A3 원점에 있음"), (155, "A1 펄스완료"), (156, "A2 펄스완료"),
              (157, "A3 펄스완료"), (200, "정지모드"), (201, "수동모드"), (202, "자동모드"),
              (203, "기동"), (21, "단일순환"), (22, "순환")):
    print(f"  M{n:<4} {nm:<14} {'ON' if n in ms else 'off'}")
print(f"\n  현재 ON인 M 전체: {sorted(ms)}")
print(f"\n{zk.report()}")
zk.close()
