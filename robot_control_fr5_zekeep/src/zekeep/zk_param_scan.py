#!/usr/bin/env python3
"""
zk_param_scan.py - ZKBOT PLC 파라미터 전수 조사 (읽기 전용)

    zk_param_scan.py --robot zkbot1 [--range 0 120]

★ 왜 일반 D 영역만 읽는가
  D8340 같은 특수 레지스터는 FX 프로그래밍 프로토콜에서 0x0E00 기준이고
  D8000~D8255 까지만 그 영역에 있다. D8340 은 범위 밖이라 일반영역 D84 와
  주소가 겹쳐 엉뚱한 값이 읽힌다(2026-08-10 실측).
  다행히 출하 래더가 파라미터를 일반 D 영역에서 특수 레지스터로 복사하므로
  (D54~D59 → D8348/8349/8358/8359/8368/8369 등), 일반 영역만 봐도 된다.

★ 체크섬 검증 + 2회 일치 확인으로 읽는다. 한 번에 많이 읽으면 무응답이 늘어
  값이 깨지므로(실측 79%), 작게 나눠 읽고 재시도한다.
"""
import sys
import time

sys.path.insert(0, "/home/ar")
from zkfx import ZK, A_D                                   # noqa: E402
from zk_safety import PULSES_PER_DEG                       # noqa: E402
import zk_profiles as ZPROF                                # noqa: E402

# 교재(3축 로봇 1~8절) + 실측으로 확인된 의미
KNOWN = {
    0:  "모드 (0=정지 1=수동 2=자동)",
    10: "원점 스텝",
    30: "현재 설입 스텝수",
    50: "속도 % (→D2020/D2024 자동속도)",
    52: "최고속도 Hz",
    54: "X(A1) 가속시간 ms → D8348",
    55: "X(A1) 감속시간 ms → D8349",
    56: "Y(A2) 가속시간 ms → D8358",
    57: "Y(A2) 감속시간 ms → D8359",
    58: "Z(A3) 가속시간 ms → D8368",
    59: "Z(A3) 감속시간 ms → D8369",
    60: "설입 속도 %",
    62: "설입 지연시간 s",
    70: "총 설입 스텝수",
    72: "펄스/회전 (세분)",
    74: "감속비",
    76: "크리프 속도 Hz",
    78: "설입 시 펌프 상태",
    90: "모드 표시(추정)",
}


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def d16(zk, n, tries=3):
    for _ in range(tries):
        b = zk.read(A_D + n * 2, 2)
        if b is not None:
            return int.from_bytes(b, "little", signed=True)
        time.sleep(0.05)
    return None


def d32(zk, n, tries=3):
    for _ in range(tries):
        b = zk.read(A_D + n * 2, 4)
        if b is not None:
            return int.from_bytes(b, "little", signed=True)
        time.sleep(0.05)
    return None


def main():
    lo = int(arg("--range", "0"))
    hi = int(sys.argv[sys.argv.index("--range") + 2]) if "--range" in sys.argv else 100

    prof = ZPROF.resolve_from_argv()
    zk = ZK(prof.port)
    if not zk.link():
        sys.exit(f"링크 실패 ({prof.name})")

    try:
        print(f"\n  ── D{lo} ~ D{hi} 스캔 (0 이 아닌 값 + 알려진 주소) ──")
        print(f"  {'주소':>6} {'16bit':>10} {'32bit':>14}   설명")
        for n in range(lo, hi + 1):
            v = d16(zk, n)
            if v is None:
                print(f"  D{n:<5} {'읽기실패':>10}")
                continue
            v32 = d32(zk, n)
            note = KNOWN.get(n, "")
            if v == 0 and not note:
                continue                    # 0이고 의미도 모르면 생략
            print(f"  D{n:<5} {v:>10} {v32 if v32 is not None else '-':>14}   {note}")

        print("\n  ── 축 각도 (float32, 래더가 계산해 넣는 값) ──")
        import struct
        for name, reg in (("A1", 1010), ("A2", 1030), ("A3", 1050)):
            b = zk.read(A_D + reg * 2, 4)
            if b is None:
                print(f"    {name}  D{reg}  읽기 실패")
                continue
            deg = struct.unpack("<f", b)[0]
            print(f"    {name}  D{reg} = {deg:9.3f}°   (= {deg*PULSES_PER_DEG:11.0f} 펄스)")
    finally:
        print(f"\n{zk.report()}")
        zk.close()


if __name__ == "__main__":
    main()
