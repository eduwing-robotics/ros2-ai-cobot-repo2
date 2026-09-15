#!/usr/bin/env python3
"""
zk_rz.py - (A1, r, z) 원통좌표 이동·조회  [3순위 과제, 2026-08-21]

왜 필요한가
  관절각 저장은 배치가 바뀌면 전부 죽는다(8/20 두 번 겪음). 사람이 원하는 건
  "5mm 내려" "10mm 앞으로"지 "A3 를 -46.958 로"가 아니다. 이 도구는 실측 피팅
  모델 위에서 r(수평거리)·z(높이)를 직접 다룬다.

    python3 zk_rz.py show                          # 현재 (A1, r, z)
    python3 zk_rz.py headroom                      # 현재 반경의 수직 범위
    python3 zk_rz.py move --dz -5                  # 수직 5mm 하강 (r 유지)
    python3 zk_rz.py move --dr +10                 # 팔 10mm 뻗기 (z 유지)
    python3 zk_rz.py move --dz 3 --dr -2           # 동시 지정
    python3 zk_rz.py goto --r 220 --z 100          # 절대 (r,z) 로
    python3 zk_rz.py goto --pose base_place --dz 15   # 저장자세 기준 상대 이동

  전부 --robot zkbot1/zkbot2 지원. A1 은 건드리지 않는다(zk_axis 로).

안전
  · zk_vlift 와 같은 잘게 나눈 스텝(기본 1.5mm) + 2단 수렴 + 무동작 검출
  · 목표가 소프트리밋 밖이면 이동 전에 거부하고, 그 반경의 가능 범위를 알려준다
  · 시작 전 원점 확보 확인
"""
import json, math, sys
sys.path.insert(0, "/home/ar")
from zkfx import ZK
from zk_safety import Guard, Abort, HOME_FLAG, JOINT_LIMITS
import zk_pose as P
import zk_profiles as ZPROF

L = 200.0
Q20, Q30 = 7.741, -18.932


def fk(a2, a3):
    q2, q3 = math.radians(Q20 - a2), math.radians(Q30 - a3)
    return (L * math.sin(q2) + L * math.cos(q3),
            L * math.cos(q2) - L * math.sin(q3))


def ik(r, z, a2g, a3g):
    """뉴턴 2변수 (zk_vlift 와 동일). 초기치는 이웃 자세. 실패 시 None."""
    a2, a3 = a2g, a3g
    for _ in range(80):
        cr, cz = fk(a2, a3)
        er, ez = r - cr, z - cz
        if abs(er) < 0.02 and abs(ez) < 0.02:
            return a2, a3
        r2, z2 = fk(a2 + 1e-3, a3)
        r3, z3 = fk(a2, a3 + 1e-3)
        j11, j12 = (r2 - cr) / 1e-3, (r3 - cr) / 1e-3
        j21, j22 = (z2 - cz) / 1e-3, (z3 - cz) / 1e-3
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-9:
            return None
        a2 += (j22 * er - j12 * ez) / det
        a3 += (-j21 * er + j11 * ez) / det
    return None


def z_range(R, tol=0.4):
    """반경 R 에서 관절범위 안 수직 z 범위 (lo, hi). 해 없으면 (None, None)."""
    lo_z = hi_z = None
    a2 = JOINT_LIMITS["A2"][0]
    while a2 <= JOINT_LIMITS["A2"][1] + 1e-9:
        lo, hi = JOINT_LIMITS["A3"]
        for _ in range(60):
            m = (lo + hi) / 2
            if fk(a2, m)[0] < R:
                lo = m
            else:
                hi = m
        a3 = (lo + hi) / 2
        r, z = fk(a2, a3)
        if abs(r - R) < tol and JOINT_LIMITS["A3"][0] <= a3 <= JOINT_LIMITS["A3"][1]:
            hi_z = z if hi_z is None or z > hi_z else hi_z
            lo_z = z if lo_z is None or z < lo_z else lo_z
        a2 += 0.1
    return lo_z, hi_z


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def in_limits(a2, a3):
    return (JOINT_LIMITS["A2"][0] <= a2 <= JOINT_LIMITS["A2"][1]
            and JOINT_LIMITS["A3"][0] <= a3 <= JOINT_LIMITS["A3"][1])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "show"
    prof = P.apply_profile(ZPROF.resolve(argv=sys.argv))
    print(f"[{prof.name}] {prof.port}", flush=True)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")
    try:
        a = P.angles(zk)
        ms = zk.m(32) or set()
        if any(b not in ms for b in HOME_FLAG.values()):
            sys.exit("■ 원점 미확보 — zk_startup.py 먼저")
        r0, z0 = fk(a["A2"], a["A3"])

        if cmd == "show":
            print(f"  A1={a['A1']:9.3f}°  A2={a['A2']:8.3f}°  A3={a['A3']:8.3f}°")
            print(f"  r={r0:7.1f} mm   z={z0:7.1f} mm")
            return
        if cmd == "headroom":
            lo, hi = z_range(r0)
            print(f"  r={r0:.1f} mm 의 수직 범위  z {lo:.1f} ~ {hi:.1f}")
            print(f"  현재 z={z0:.1f}  →  위로 +{hi-z0:.1f} mm / 아래로 {z0-lo:.1f} mm")
            return

        # ── 목표 (r,z) 결정 ──
        if cmd == "move":
            rt = r0 + float(arg("--dr", "0"))
            zt = z0 + float(arg("--dz", "0"))
        elif cmd == "goto":
            pose = arg("--pose")
            if pose:
                db = P.load()
                if pose not in db:
                    sys.exit(f"■ 자세 '{pose}' 없음")
                pr, pz = fk(db[pose]["A2"], db[pose]["A3"])
                rt = pr + float(arg("--dr", "0"))
                zt = pz + float(arg("--dz", "0"))
            else:
                rt = float(arg("--r", str(r0)))
                zt = float(arg("--z", str(z0)))
        else:
            sys.exit(__doc__)

        tgt = ik(rt, zt, a["A2"], a["A3"])
        if tgt is None or not in_limits(*tgt):
            lo, hi = z_range(rt)
            msg = f"  r={rt:.1f} 의 가능 z 범위: {lo:.1f} ~ {hi:.1f}" if lo else \
                  f"  r={rt:.1f} 는 관절범위 안에 수직 해가 없음"
            sys.exit(f"■ 목표 (r {rt:.1f}, z {zt:.1f}) 도달 불가\n{msg}")

        # ── 잘게 나눠 이동 (직선 보간, zk_vlift 방식) ──
        step = float(arg("--step", "1.5"))
        speed = float(arg("--speed", "5"))
        fine = float(arg("--fine-lead", "0.08"))
        dist = math.hypot(rt - r0, zt - z0)
        n = max(1, int(math.ceil(dist / step)))
        print(f"  ({r0:.1f},{z0:.1f}) → ({rt:.1f},{zt:.1f})  {dist:.1f} mm, {n} 스텝, {speed:g}%")
        guard = Guard(zk, homing=False)
        guard.preflight(a)
        P.set_speed(zk, speed)
        lead = max(speed * 0.083, 0.2)
        ph = ((speed, lead), (2.0, fine))
        a2c, a3c = a["A2"], a["A3"]
        prev = dict(a)
        for i in range(1, n + 1):
            t = i / n
            sol = ik(r0 + (rt - r0) * t, z0 + (zt - z0) * t, a2c, a3c)
            if sol is None:
                raise Abort("IK 미수렴(중간점)")
            a2c, a3c = sol
            P.goto_axis(zk, guard, "A2", a2c, 0.2, ph)
            P.goto_axis(zk, guard, "A3", a3c, 0.2, ph)
            cur = P.angles(zk)
            rn, zn = fk(cur["A2"], cur["A3"])
            moved = max(abs(cur["A2"] - prev["A2"]), abs(cur["A3"] - prev["A3"]))
            flag = "" if moved > 0.05 or dist / n < 0.5 else "  🔴 무동작 의심"
            print(f"  wp{i:2d}/{n}  r{rn - r0:+6.1f} z{zn - z0:+6.1f}  "
                  f"(목표대비 Δr {rn - (r0 + (rt - r0) * t):+.2f} "
                  f"Δz {zn - (z0 + (zt - z0) * t):+.2f}){flag}", flush=True)
            prev = cur
        cur = P.angles(zk)
        rn, zn = fk(cur["A2"], cur["A3"])
        print(f"  ── 최종  r={rn:.1f} (목표 {rt:.1f}, Δ{rn - rt:+.2f})   "
              f"z={zn:.1f} (목표 {zt:.1f}, Δ{zn - zt:+.2f})")
    except Abort as e:
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
