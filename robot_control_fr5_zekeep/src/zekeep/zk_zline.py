#!/usr/bin/env python3
"""
zk_zline.py - 팔끝 수직 직선 이동 (역기구학 경유점, 2026-08-12 실증)

    python3 zk_zline.py --robot zkbot1 --z-to 80            # 현재 팔모양에서 z=80mm 로 직하강
    python3 zk_zline.py --z-to 80 --z-step 7 --speed 8

★기구학 모델 (평행사변형 링크 — 흡착판 항상 하향):
    r = L1·sin(q2_0 - A2) + L2·cos(q3_0 - A3)     [mm, A1축에서 수평거리]
    z = L1·cos(q2_0 - A2) - L2·sin(q3_0 - A3)     [mm]
  L1=L2=200(도면 실측). q2_0=7.741 / q3_0=-18.932 은 8/12 역산:
  실물 검증된 수직 2구간(pick→lift, lift→place)의 r 불변 조건으로 풀었고,
  pick/lift/place 가 전부 r=246.2mm 한 수직선에 얹힘 + 원점 포함각이
  도면(150.75°)과 2.5° 이내로 정합. 실증: 105mm 하강에서 r편차 ±2mm.

주의: A1 회전은 안 다룬다(수직선이므로 무관). 하강 하한은 기본 z=70mm
  (pick 접촉면 z=56.2 + 여유) — 그 밑은 --force 로만.
"""
import math
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort                     # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402

L1 = L2 = 200.0
Q20, Q30 = 7.741, -18.932


def fk(a2, a3):
    q2, q3 = math.radians(Q20 - a2), math.radians(Q30 - a3)
    return (L1 * math.sin(q2) + L2 * math.cos(q3),
            L1 * math.cos(q2) - L2 * math.sin(q3))


def ik(r, z, a2g, a3g):
    """뉴턴 2변수. 초기치는 이웃 자세(연속 경로 전제)."""
    a2, a3 = a2g, a3g
    for _ in range(60):
        cr, cz = fk(a2, a3)
        er, ez = r - cr, z - cz
        if abs(er) < 0.02 and abs(ez) < 0.02:
            return a2, a3
        r2, z2 = fk(a2 + 1e-3, a3)
        r3, z3 = fk(a2, a3 + 1e-3)
        j11, j12 = (r2 - cr) / 1e-3, (r3 - cr) / 1e-3
        j21, j22 = (z2 - cz) / 1e-3, (z3 - cz) / 1e-3
        det = j11 * j22 - j12 * j21
        a2 += (j22 * er - j12 * ez) / det
        a3 += (-j21 * er + j11 * ez) / det
    raise Abort("IK 미수렴")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    z_to = float(arg("--z-to", "80"))
    z_step = float(arg("--z-step", "7"))
    speed = float(arg("--speed", "8"))
    z_floor = 70.0
    if z_to < z_floor and "--force" not in sys.argv:
        sys.exit(f"■ z {z_to} < 하한 {z_floor} (pick 접촉면 56.2 보호) — --force 필요")

    prof = P.apply_profile(ZPROF.resolve_from_argv())
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")
    try:
        g = Guard(zk, homing=False)
        a = P.angles(zk)
        g.preflight(a)
        r0, z0 = fk(a["A2"], a["A3"])
        print(f"  시작 r={r0:.1f} z={z0:.1f} → z={z_to} (스텝 {z_step}mm, {speed:g}%)", flush=True)
        a2, a3 = a["A2"], a["A3"]
        z = z0
        sgn = 1.0 if z_to > z0 else -1.0
        n = 0
        while (z_to - z) * sgn > 0.01:
            z = z_to if abs(z_to - z) <= z_step else z + sgn * z_step
            a2, a3 = ik(r0, z, a2, a3)
            P.goto_axis(zk, g, "A2", a2, 0.25, ((speed, 0.6),))
            P.goto_axis(zk, g, "A3", a3, 0.25, ((speed, 0.6),))
            cur = P.angles(zk)
            rn, zn = fk(cur["A2"], cur["A3"])
            n += 1
            print(f"  wp{n:2d}  z {zn:6.1f}  r편차 {rn-r0:+.1f}mm", flush=True)
        print("  완료", flush=True)
    except Abort as e:
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
