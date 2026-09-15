#!/usr/bin/env python3
"""
zk_teach_dump.py - PLC 에 저장된 티칭 데이터 읽기 (읽기 전용)

    zk_teach_dump.py --robot zkbot1 [--save 파일.json]

제조사 래더(3축 로봇 교재 5·8절) 기준 레지스터 맵 — 스텝 n 은 32비트라 2씩 증가:

    D70          총 설입 스텝수
    D30          현재 설입 스텝수
    D0           모드 (0=정지 1=수동 2=자동)
    D100 + 2n    A1(X축) 목표 펄스   ← 32비트
    D200 + 2n    A2(Y축) 목표 펄스
    D300 + 2n    A3(Z축) 목표 펄스
    D400 + 2n    속도 %
    D450 + 2n    지연시간 (100ms 단위)
    D150 + 2n    펌프 상태 (1=ON, 0=OFF)

    D8340/D8350/D8360  각 축 현재 펄스 (32비트, 읽기 전용)

각도 = 펄스 × 360 / (펄스per회전 × 감속비) = 펄스 / 177.78
"""
import json
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D                                   # noqa: E402
from zk_safety import PULSES_PER_DEG                       # noqa: E402
import zk_profiles as ZPROF                                # noqa: E402

MODE = {0: "정지", 1: "수동", 2: "자동"}


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def d32(zk, n):
    """D(n), D(n+1) 을 32비트 부호있는 정수로 읽는다 (FX 는 리틀엔디언)."""
    b = zk.read(A_D + n * 2, 4)
    if b is None:
        return None
    return int.from_bytes(b, "little", signed=True)


def d16(zk, n):
    b = zk.read(A_D + n * 2, 2)
    if b is None:
        return None
    return int.from_bytes(b, "little", signed=True)


def main():
    prof = ZPROF.resolve_from_argv()
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")

    out = {"robot": prof.name, "read_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        mode = d16(zk, 0)
        total = d16(zk, 70)
        cur = d16(zk, 30)
        print(f"[{prof.name}] {prof.port}")
        print(f"  D0  모드      = {mode} ({MODE.get(mode, '?')})")
        print(f"  D70 총 스텝수 = {total}")
        print(f"  D30 현재 스텝 = {cur}")
        out.update(mode=mode, total_steps=total, cur_step=cur)

        # 현재 실제 위치 (특수 레지스터)
        print("\n  ── 현재 축 펄스 (D8340/8350/8360) ──")
        now = {}
        for name, reg in (("A1", 8340), ("A2", 8350), ("A3", 8360)):
            p = d32(zk, reg)
            deg = None if p is None else p / PULSES_PER_DEG
            now[name] = {"pulse": p, "deg": deg}
            print(f"    {name}  {p:>10} 펄스  = {deg:8.3f}°" if p is not None
                  else f"    {name}  읽기 실패")
        out["current"] = now

        if not total or total <= 0:
            print("\n  저장된 티칭 스텝이 없습니다.")
            return

        print(f"\n  ── 저장된 티칭 스텝 {total}개 ──")
        print(f"  {'스텝':>4} {'A1(°)':>10} {'A2(°)':>10} {'A3(°)':>10} "
              f"{'속도%':>6} {'지연s':>6} {'펌프':>5}")
        steps = []
        for i in range(total):
            off = 2 * i
            a1 = d32(zk, 100 + off)
            a2 = d32(zk, 200 + off)
            a3 = d32(zk, 300 + off)
            spd = d16(zk, 400 + off)
            dly = d16(zk, 450 + off)
            pmp = d16(zk, 150 + off)
            row = {"step": i + 1,
                   "A1_pulse": a1, "A2_pulse": a2, "A3_pulse": a3,
                   "A1_deg": None if a1 is None else a1 / PULSES_PER_DEG,
                   "A2_deg": None if a2 is None else a2 / PULSES_PER_DEG,
                   "A3_deg": None if a3 is None else a3 / PULSES_PER_DEG,
                   "speed_pct": spd, "delay_100ms": dly, "pump": pmp}
            steps.append(row)
            f = lambda v: "    ----" if v is None else f"{v:10.3f}"   # noqa: E731
            print(f"  {i+1:>4} {f(row['A1_deg'])} {f(row['A2_deg'])} {f(row['A3_deg'])} "
                  f"{spd if spd is not None else '--':>6} "
                  f"{(dly/10 if dly is not None else 0):>6.1f} "
                  f"{'ON' if pmp else 'off':>5}")
        out["steps"] = steps

        path = arg("--save")
        if path:
            json.dump(out, open(path, "w"), indent=2, ensure_ascii=False)
            print(f"\n  → 백업 저장: {path}")
    finally:
        print(f"\n{zk.report()}")
        zk.close()


if __name__ == "__main__":
    main()
