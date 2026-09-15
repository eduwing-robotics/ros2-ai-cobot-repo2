#!/usr/bin/env python3
"""
zk_seek_limit.py - 리밋 스위치를 찾을 때까지 조그해서 다가간다

용도
  원점복귀(DZRN)가 한 방향으로 계속 밀어도 리밋을 못 만나는 경우,
  어느 방향에 리밋이 있는지를 조그로 직접 찾는다.
  찾으면 그 지점에서 원점복귀를 걸면 즉시 성공한다.

안전
  · 조그는 짧은 펄스(기본 0.5초) + 매회 무조건 전 비트 OFF
  · try/finally 로 관련 M비트 전부 해제
  · 방향당 누적 이동 상한 (기본 100°) — 넘으면 방향 전환, 둘 다 실패면 중단
  · X5(급정지) 즉시 중단
  · 리밋 ON 되는 순간 즉시 정지

    python3 zk_seek_limit.py A1 [--prefer-positive|--prefer-negative] [--cap 100]
"""
import struct
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D  # noqa: E402
import zk_profiles as ZPROF  # noqa: E402

AXIS = {
    "A1": (0, 1010, (1, 31, 101), (2, 103)),
    "A2": (1, 1030, (3, 41, 105), (4, 107)),
    "A3": (2, 1050, (5, 51, 109), (6, 111)),
}
ALL_JOG = tuple(range(1, 13)) + (31, 33, 41, 43, 51, 53) + tuple(range(101, 113))
PULSE_S = 0.5


def ang(zk, d):
    b = zk.read(A_D + d * 2, 4)
    return None if b is None else struct.unpack("<f", b)[0]


def all_off(zk):
    for _ in range(2):
        for b in ALL_JOG:
            zk.m_off(b)


def run_dir(zk, label, bits, xno, dno, cap):
    """한 방향으로 리밋을 찾을 때까지 조그. (찾음?, 부호, 누적이동) 반환."""
    print(f"\n── {label} 탐색 (비트 {list(bits)}, 상한 {cap}°) ──", flush=True)
    a0 = ang(zk, dno)
    total = 0.0
    sign = None
    n = 0
    while abs(total) < cap:
        before = ang(zk, dno)
        for b in bits:
            zk.m_on(b)
        t = time.time()
        while time.time() - t < PULSE_S:
            time.sleep(0.05)
        all_off(zk)
        n += 1
        time.sleep(0.15)

        xs = zk.x() or set()
        after = ang(zk, dno)
        if 5 in xs:
            print("  ■ X5(급정지) 감지 — 중단", flush=True)
            return None, sign, total

        if before is not None and after is not None:
            d = after - before
            if sign is None and abs(d) > 0.05:
                sign = "+" if d > 0 else "-"
            total = after - a0

        print(f"  {n:>2}회  {after:9.2f}°  누적 {total:+7.2f}°  "
              f"X{xno}={'ON' if xno in xs else 'OFF'}", flush=True)

        if xno in xs:
            print(f"\n  ★★ 리밋 발견 — X{xno} ON  (방향 {label}, 부호 {sign})", flush=True)
            return True, sign, total

        if before is not None and after is not None and abs(after - before) < 0.05:
            print("  각도가 안 변함 — 이 방향은 무효", flush=True)
            return False, sign, total
    print(f"  상한 {cap}° 도달 — 이 방향엔 리밋 없음", flush=True)
    return False, sign, total


def main():
    axis = sys.argv[1] if len(sys.argv) > 1 else "A1"
    cap = 100.0
    if "--cap" in sys.argv:
        cap = float(sys.argv[sys.argv.index("--cap") + 1])
    xno, dno, fwd, rev = AXIS[axis]

    zk = ZK(ZPROF.resolve_from_argv().port)
    if not zk.link():
        sys.exit("링크 실패")
    try:
        xs = zk.x() or set()
        if 5 in xs:
            sys.exit("X5(급정지)가 눌려 있음")
        if xno in xs:
            print(f"{axis} 리밋(X{xno})이 이미 ON입니다. 바로 원점복귀 가능.")
            return
        print(f"{axis} 리밋 탐색 시작   현재 {ang(zk,dno):.2f}°", flush=True)
        all_off(zk)

        # 원점복귀는 카운터 음수 방향으로 몰았고 실패했다 → 반대편부터 본다
        for label, bits in (("정방향", fwd), ("역방향", rev)):
            found, sign, tot = run_dir(zk, label, bits, xno, dno, cap)
            if found:
                print(f"\n  → 이제 원점복귀를 걸면 즉시 잡힙니다.", flush=True)
                return
        print("\n  두 방향 모두 리밋 없음 → X0 스위치/배선 의심", flush=True)
    finally:
        all_off(zk)
        ms = zk.m(32) or set()
        xs = zk.x() or set()
        stuck = [b for b in ALL_JOG if b in ms]
        print("\n── 마무리 ──", flush=True)
        print(f"  비트 해제 {'✅ 잔류 없음' if not stuck else f'🔴 잔류 {stuck}'}", flush=True)
        print(f"  각도 {ang(zk,dno):.2f}°   리밋 X0={0 in xs} X1={1 in xs} X2={2 in xs}",
              flush=True)
        print(f"\n{zk.report()}", flush=True)
        zk.close()


if __name__ == "__main__":
    main()
