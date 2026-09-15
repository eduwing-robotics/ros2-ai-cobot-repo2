#!/usr/bin/env python3
"""
zk_a1snap.py - A1 미세펄스 정밀 스냅  [2026-08-21]

왜: A1 은 yaw 이자 원주위치라 오차가 r 배로 증폭된다(r 268mm 에서 0.1°=0.47mm).
    goto_axis 의 2단(2%, lead 0.15°)로는 0.07~0.16° 가 남는다.
    A2/A3 를 0.02mm 로 만든 처방(미세 스텝+되먹임)을 A1 에도 적용:
    ★펄스 '폭'을 0.30→0.15→0.08→0.04s 로 줄여가며 수렴 — 최소 이동량이
    폭에 비례하므로 잔차 하한도 같이 내려간다. 매 펄스 실측 되먹임.

    python3 zk_a1snap.py --robot zkbot1 --target -180.0 [--tol 0.02]
"""
import sys, time
sys.path.insert(0, "/home/ar")
from zkfx import ZK
from zk_safety import Guard, Abort, HOME_FLAG, JOINT_LIMITS
import zk_pose as P
import zk_profiles as ZPROF

def arg(n, d=None):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d

def main():
    prof = P.apply_profile(ZPROF.resolve(argv=sys.argv))
    tgt = float(arg("--target"))
    tol = float(arg("--tol", "0.02"))
    lo, hi = JOINT_LIMITS["A1"]
    if not (lo <= tgt <= hi):
        sys.exit(f"■ 목표 {tgt}° 가 소프트리밋({lo}~{hi}) 밖")
    print(f"[{prof.name}] A1 → {tgt:.3f}°  (tol {tol}°)", flush=True)
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit("링크 실패")
    POS, NEG = prof.bits["A1"]
    try:
        ms = zk.m(32) or set()
        if any(b not in ms for b in HOME_FLAG.values()):
            sys.exit("■ 원점 미확보")
        guard = Guard(zk, homing=False)
        guard.preflight(P.angles(zk))
        P.set_speed(zk, 2)                     # 2% = 1.1°/s (거친 구간)
        widths = (0.30, 0.15, 0.08, 0.04)      # 펄스 폭 사다리
        wi, stall, fine_speed = 0, 0, False
        for i in range(40):
            a = P.angles(zk)
            err = tgt - a["A1"]                # + 면 정방향 필요
            if abs(err) <= tol:
                print(f"  ✅ 수렴  A1={a['A1']:.3f}°  잔차 {err:+.3f}°  ({i}펄스)")
                return
            # 예상 이동량이 |err| 보다 크면 폭을 한 단계 내린다
            while wi < len(widths) - 1 and 1.1 * widths[wi] > abs(err) * 1.6:
                wi += 1
            # ★2026-08-21 실측: 폭을 줄여도 최소 이동량이 ~0.17° 에서 바닥
            #   (통신지연분 = 속도×0.15s 가 폭과 무관). 미세 구간(|err|<0.2°)에서는
            #   속도를 1% 로 내려 바닥 자체를 절반(~0.09°)으로 낮춘다 → 핑퐁 방지.
            if abs(err) < 0.2 and not fine_speed:
                P.set_speed(zk, 1); fine_speed = True
                print("  ↓ 미세구간 — 속도 1% 로", flush=True)
            bits = POS if err > 0 else NEG
            prev = a["A1"]
            P.pulse(zk, bits, widths[wi])
            cur = P.angles(zk)["A1"]
            moved = cur - prev
            print(f"  p{i+1:2d} w={widths[wi]:.2f}s  {prev:8.3f}→{cur:8.3f} "
                  f"({moved:+.3f}°)  잔차 {tgt-cur:+.3f}°", flush=True)
            if abs(moved) < 0.005:
                stall += 1
                if stall >= 2 and wi < len(widths) - 1:
                    wi_old, wi = wi, wi   # 폭이 너무 좁아 정지마찰을 못 이김 → 한 단계 되올림
                    wi = max(0, wi - 1); stall = 0
                    print(f"  ↺ 무동작 2회 — 폭 {widths[wi]:.2f}s 로 되올림")
                elif stall >= 4:
                    print(f"  ■ 정지마찰 한계 — 잔차 {tgt-cur:+.3f}° 에서 종료"); return
            else:
                stall = 0
        a = P.angles(zk)
        print(f"  ■ 40펄스 소진  A1={a['A1']:.3f}°  잔차 {tgt-a['A1']:+.3f}°")
    except Abort as e:
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()

if __name__ == "__main__":
    main()
