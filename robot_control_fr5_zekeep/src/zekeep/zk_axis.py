#!/usr/bin/env python3
"""
zk_axis.py - 단일 축 절대각 이동 (티칭 보조)

    python3 zk_axis.py --robot zkbot1 A1 -97.74 --speed 10
    python3 zk_axis.py --robot zkbot1 A3 -60 --speed 5 --tol 0.15

한 축만 움직인다 — 부품을 든 채 A1 회전 등 재티칭 작업용.
Guard 가 소프트리밋·X5·통신두절을 감시한다.
"""
import sys

sys.path.insert(0, "/home/ar")
from zkfx import ZK                                    # noqa: E402
from zk_safety import Guard, Abort                     # noqa: E402
import zk_pose as P                                    # noqa: E402
import zk_profiles as ZPROF                            # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith("--")
           and a not in (arg("--robot"), arg("--port"), arg("--speed"), arg("--tol"))]
    if len(pos) < 2 or pos[0] not in ("A1", "A2", "A3"):
        sys.exit("사용법: zk_axis.py [--robot NAME] A1|A2|A3 <절대각도> [--speed N] [--tol N]")
    joint, target = pos[0], float(pos[1])
    speed = float(arg("--speed", "10"))
    tol = float(arg("--tol", "0.3"))

    prof = P.apply_profile(ZPROF.resolve_from_argv())
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")
    lead = max(speed * 0.083, 0.3)
    phases = ((speed, lead), (8, 0.6))
    try:
        a0 = P.angles(zk)
        print(f"  시작 {P.fmt(a0)}  →  {joint} {target:+.2f}°", flush=True)
        guard = Guard(zk, homing=False)
        guard.preflight(a0)
        P.goto_axis(zk, guard, joint, target, tol, phases)
        a = P.angles(zk)
        print(f"  도착 {P.fmt(a)}  ({joint} 오차 {a[joint]-target:+.3f}°)", flush=True)
    except Abort as e:
        sys.exit(f"■ 안전 중단: {e}")
    finally:
        P.all_off(zk)
        zk.close()


if __name__ == "__main__":
    main()
