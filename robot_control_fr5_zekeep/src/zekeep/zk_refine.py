#!/usr/bin/env python3
"""
zk_refine.py - 티칭 자세 정밀 다듬 (pick/place 도착 후 잔차 제거)

    python3 zk_refine.py                      # base_pick, A3->A2->A1
    python3 zk_refine.py --robot zkbot1 --pose base_pick --tol 0.15

zk_axis(goto_axis 기본 lead 0.415°)는 goto 착지 잔차(0.2~0.35°)가 lead 안이면
아예 시도하지 않는다(2026-08-12 실증: 3축 전부 무동작 종료). zk_carry ①정밀상승이
실물 검증한 2단계 phase — 두 번째 lead 를 max(lead*0.4, 0.15) 까지 좁힘 — 를
그대로 사용해 작은 잔차도 다듬는다.

A3->A2->A1 순서 (2026-08-11 확립: A1 다듬은 놓기 yaw 랜덤 성분 제거).
"""
import json
import sys

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort                     # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    pose_n = arg("--pose", "base_pick")
    speed = float(arg("--speed", "5"))
    tol = float(arg("--tol", "0.15"))

    prof = P.apply_profile(ZPROF.resolve_from_argv())
    try:
        with open(prof.pose_file) as f:
            poses = json.load(f)
    except FileNotFoundError:
        sys.exit(f"■ 자세DB 없음: {prof.pose_file}")
    if pose_n not in poses:
        sys.exit(f"■ 티칭 자세 '{pose_n}' 없음 — zk_pose.py save 필요")
    tgt = poses[pose_n]

    # 통신지연 150ms 가 곧 정지 관성: 5%(2.8도/s)≈0.42도, 2%(1.1도/s)≈0.17도.
    # 5% 단일 패스는 반대편 오버슛으로 끝난다(2026-08-12 실증: 0.2→0.46도 악화).
    # → 잔차가 크면 5%, 작아지면 2% 로 낮춰 왕복 수렴. 축당 최대 5회.
    ATTEMPTS = (5.0, 2.0, 2.0, 1.5, 1.5)

    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")
    try:
        a0 = P.angles(zk)
        print(f"  시작 {P.fmt(a0)}  →  '{pose_n}' 다듬 (tol {tol})", flush=True)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)
        for j in ("A3", "A2", "A1"):
            for spd in ATTEMPTS:
                cur = P.angles(zk)[j]
                err = tgt[j] - cur
                if abs(err) <= tol:
                    break
                if abs(err) > 0.6:          # 큰 잔차만 5%, 그 외 저속
                    spd = max(spd, 5.0)
                lead = min(0.15, tol)
                P.goto_axis(zk, guard, j, tgt[j], tol, ((spd, lead),))
        a = P.angles(zk)
        errs = {j: a[j] - tgt[j] for j in ("A3", "A2", "A1")}
        ok = all(abs(e) <= tol for e in errs.values())
        print(f"  최종 {P.fmt(a)}", flush=True)
        print("  잔차  " + "  ".join(f"{j} {e:+.3f}°" for j, e in errs.items())
              + ("  ✅" if ok else "  ⚠ tol 초과"), flush=True)
        sys.exit(0 if ok else 1)
    except Abort as e:
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
